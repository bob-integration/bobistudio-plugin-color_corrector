# ─────────────────────────────────────────────────────────────────────────────
# Color corrector plugin — corrige un flux vidéo YUV420p du pipeline MXL (luma/
# chroma + per-channel gamma + color balance), réglable À CHAUD via :8082.
#   Entrée  : input_shm (câblable à chaud, mode hot-wire)
#   Sortie  : {hostname}_cc
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import mmap, struct, time, threading, json, os, signal
from collections import deque
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─── Latence : Δ ts_output - ts_input (rolling avg) ────────────
class RollingMs:
    def __init__(self, n=30):
        self.d = deque(maxlen=n); self.last_ns = 0
    def push(self, ms_value):
        self.d.append(ms_value); self.last_ns = time.time_ns()
    def avg(self):
        if not self.d: return None
        if time.time_ns() - self.last_ns > 2_000_000_000: return None
        return round(sum(self.d) / len(self.d), 1)

lat_in = RollingMs()

# ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

SHM_OUT     = CONFIG.get("shm_out") or (HOSTNAME + "_cc")
WIDTH       = int(CONFIG.get("width") or 1280);  WIDTH  -= WIDTH % 2
HEIGHT      = int(CONFIG.get("height") or 720);  HEIGHT -= HEIGHT % 2
FPS         = int(round(float(CONFIG.get("fps") or 25))) or 25
INITIAL_INPUT_SHM = (CONFIG.get("input_shm") or None)     # str ou None
INITIAL_PARAMS    = CONFIG.get("cc_params") or {{}}        # dict

# ─── Format yuv420p/422p/444p (selon CONFIG[chroma], défaut 4:2:2) ──
CHROMA = str(CONFIG.get("chroma") or "422")
_CW = {{"420": 2, "422": 2, "444": 1}}.get(CHROMA, 2)   # diviseur largeur chroma
_CH = {{"420": 2, "422": 1, "444": 1}}.get(CHROMA, 1)   # diviseur hauteur chroma
PIX_FMT = {{"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"}}.get(CHROMA, "yuv422p")
UV_W = WIDTH // _CW
UV_H = HEIGHT // _CH
V_HEADER_SIZE = 64
V_RING_SIZE   = 10
Y_SIZE        = WIDTH * HEIGHT
UV_SIZE       = UV_W * UV_H
V_FRAME_SIZE  = Y_SIZE + 2 * UV_SIZE
V_TOTAL_SIZE  = V_HEADER_SIZE + V_RING_SIZE * V_FRAME_SIZE

DEFAULT_PARAMS = {{
    "brightness": 0.0,   # -1..1
    "contrast":   1.0,   # 0..2
    "saturation": 1.0,   # 0..3
    "gamma":      1.0,   # 0.1..10
    "hue":        0.0,   # deg, -180..180
    "gamma_r":    1.0,   # 0.1..10
    "gamma_g":    1.0,
    "gamma_b":    1.0,
    # Colorbalance : -1..1 par canal × 3 zones (shadows/mids/highlights)
    "cb_rs": 0.0, "cb_gs": 0.0, "cb_bs": 0.0,
    "cb_rm": 0.0, "cb_gm": 0.0, "cb_bm": 0.0,
    "cb_rh": 0.0, "cb_gh": 0.0, "cb_bh": 0.0,
}}

def _norm_params(d):
    """Merge d sur DEFAULT_PARAMS, coercion float, valeurs hors plage ignorées."""
    out = dict(DEFAULT_PARAMS)
    for k, v in (d or {{}}).items():
        if k in DEFAULT_PARAMS:
            try: out[k] = float(v)
            except (TypeError, ValueError): pass
    return out

# ─── État runtime ───────────────────────────────────────────
state_lock = threading.Lock()
state = {{
    "input_shm": INITIAL_INPUT_SHM,
    "params":    _norm_params(INITIAL_PARAMS),
}}

# Métriques
metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}}, "plugin_version": PLUGIN_VERSION}}

# SIGBUS
bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    print("SIGBUS reçu — réouverture des mmap")
    bus_error.set()
signal.signal(signal.SIGBUS, _handle_sigbus)


# ─── HTTP : metrics 8080 + control 8082 ─────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock: payload = dict(metrics)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
    def log_message(self, *a): pass


class ControlHandler(BaseHTTPRequestHandler):
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n: return {{}}
        try: return json.loads(self.rfile.read(n).decode())
        except Exception: return {{}}

    def _reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path == "/state":
            with state_lock:
                self._reply(200, {{
                    "input_shm": state["input_shm"],
                    "shm_out":   SHM_OUT,
                    "params":    dict(state["params"]),
                    "defaults":  dict(DEFAULT_PARAMS),
                    "plugin_version": PLUGIN_VERSION,
                }})
        else:
            self._reply(404, {{"error": "not found"}})

    def do_POST(self):
        body = self._read_json()
        if self.path == "/params":
            # PATCH : merge des clés fournies (les autres restent inchangées).
            with state_lock:
                cur = dict(state["params"])
                for k, v in (body or {{}}).items():
                    if k in DEFAULT_PARAMS:
                        try: cur[k] = float(v)
                        except (TypeError, ValueError): pass
                state["params"] = cur
            self._reply(200, {{"ok": True}})
        elif self.path == "/reset":
            with state_lock:
                state["params"] = dict(DEFAULT_PARAMS)
            self._reply(200, {{"ok": True}})
        elif self.path == "/input":
            # Schéma générique plugin {{essence, shm}} — `essence` ignoré (1 entrée vidéo).
            shm = body.get("shm")
            shm = (shm or "").strip() or None
            with state_lock:
                state["input_shm"] = shm
            self._reply(200, {{"ok": True}})
        else:
            self._reply(404, {{"error": "not found"}})

    def log_message(self, *a): pass


threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(),
                 daemon=True).start()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(),
                 daemon=True).start()


# ─── Lecture / écriture shm ─────────────────────────────────
input_handle = None   # (file, mmap, name_cached) ou None

def ensure_input():
    """(Re-)ouvre l'input shm si le nom a changé. None si non câblé / indispo."""
    global input_handle
    with state_lock:
        wanted = state["input_shm"]
    if not wanted:
        if input_handle is not None:
            try: input_handle[1].close(); input_handle[0].close()
            except Exception: pass
            input_handle = None
        return None
    if input_handle is not None and input_handle[2] == wanted:
        return input_handle[1]
    if input_handle is not None:
        try: input_handle[1].close(); input_handle[0].close()
        except Exception: pass
        input_handle = None
    path = f"/dev/shm/{{wanted}}"
    try:
        if not os.path.exists(path): return None
        if os.path.getsize(path) < V_TOTAL_SIZE: return None
        f = open(path, "r+b")
        shm = mmap.mmap(f.fileno(), V_TOTAL_SIZE, prot=mmap.PROT_READ)
        _ = shm[0:16]
        input_handle = (f, shm, wanted)
        print(f"input câblé sur {{path}}")
        return shm
    except Exception as e:
        print(f"input ({{wanted}}) indisponible : {{e}}")
        return None

def lire_frame_yuv(shm):
    """Renvoie (yuv_bytes, ts_in_ns) ou (None, None)."""
    if shm is None: return None, None
    try:
        frame_index, ts_in = struct.unpack("QQ", shm[0:16])
        if frame_index == 0: return None, None
        slot = frame_index % V_RING_SIZE
        offset = V_HEADER_SIZE + slot * V_FRAME_SIZE
        return bytes(shm[offset:offset + V_FRAME_SIZE]), ts_in
    except Exception:
        return None, None


# ─── Color correction (numpy) ───────────────────────────────
def split_yuv(yuv_bytes):
    """yuv (420p/422p/444p selon chroma) bytes → (Y, U, V) numpy uint8 arrays."""
    y = np.frombuffer(yuv_bytes[:Y_SIZE], dtype=np.uint8).reshape(HEIGHT, WIDTH).copy()
    u = np.frombuffer(yuv_bytes[Y_SIZE:Y_SIZE+UV_SIZE], dtype=np.uint8).reshape(UV_H, UV_W).copy()
    v = np.frombuffer(yuv_bytes[Y_SIZE+UV_SIZE:],       dtype=np.uint8).reshape(UV_H, UV_W).copy()
    return y, u, v

def join_yuv(y, u, v):
    return y.tobytes() + u.tobytes() + v.tobytes()

def yuv_to_rgb(y, u, v):
    """Y(H,W) + U,V(H/2,W/2) → RGB float32 (H,W,3) en 0..255."""
    yf = y.astype(np.float32)
    uf = u.repeat(_CH, axis=0).repeat(_CW, axis=1).astype(np.float32)
    vf = v.repeat(_CH, axis=0).repeat(_CW, axis=1).astype(np.float32)
    c = yf - 16.0; d = uf - 128.0; e = vf - 128.0
    r = 1.164*c              + 1.596*e
    g = 1.164*c - 0.392*d    - 0.813*e
    b = 1.164*c + 2.017*d
    return np.stack([np.clip(r,0,255), np.clip(g,0,255), np.clip(b,0,255)], axis=2)

def rgb_to_yuv(rgb):
    """RGB float32 (H,W,3) → (Y(H,W), U(H/2,W/2), V(H/2,W/2)) uint8."""
    r = rgb[:,:,0]; g = rgb[:,:,1]; b = rgb[:,:,2]
    y = ( 0.257*r + 0.504*g + 0.098*b + 16.0)
    u = (-0.148*r - 0.291*g + 0.439*b + 128.0)
    v = ( 0.439*r - 0.368*g - 0.071*b + 128.0)
    y_u8 = np.clip(y, 0, 255).astype(np.uint8)
    u_u8 = np.clip(u[::_CH,::_CW], 0, 255).astype(np.uint8)
    v_u8 = np.clip(v[::_CH,::_CW], 0, 255).astype(np.uint8)
    return y_u8, u_u8, v_u8

def appliquer_correction(yuv_bytes, p):
    """Applique les params de correction. Renvoie yuv (selon chroma) bytes."""
    y, u, v = split_yuv(yuv_bytes)

    # ─ Path YUV (gratuit, ops sur planes Y et U/V uniquement) ─
    yf = y.astype(np.float32)
    uf = u.astype(np.float32) - 128.0
    vf = v.astype(np.float32) - 128.0

    # Brightness : décalage en luma (-1..1 → -128..128)
    if p["brightness"] != 0.0:
        yf = yf + p["brightness"] * 128.0
    # Contrast : (Y-128)*c + 128
    if p["contrast"] != 1.0:
        yf = (yf - 128.0) * p["contrast"] + 128.0
    # Saturation : multiplie chroma
    if p["saturation"] != 1.0:
        uf = uf * p["saturation"]
        vf = vf * p["saturation"]
    # Gamma global sur Y
    if p["gamma"] != 1.0 and p["gamma"] > 0:
        yn = np.clip(yf, 0, 255) / 255.0
        yf = np.power(yn, 1.0 / p["gamma"]) * 255.0
    # Hue rotation : rotation 2D du plan (U, V)
    if p["hue"] != 0.0:
        rad = p["hue"] * np.pi / 180.0
        cs, sn = np.cos(rad), np.sin(rad)
        u_new = uf * cs - vf * sn
        v_new = uf * sn + vf * cs
        uf, vf = u_new, v_new

    y = np.clip(yf, 0, 255).astype(np.uint8)
    u = np.clip(uf + 128.0, 0, 255).astype(np.uint8)
    v = np.clip(vf + 128.0, 0, 255).astype(np.uint8)

    # ─ Path RGB (slow) : seulement si gamma R/G/B ou colorbalance non-identité ─
    need_rgb = (
        p["gamma_r"] != 1.0 or p["gamma_g"] != 1.0 or p["gamma_b"] != 1.0 or
        any(p[k] != 0.0 for k in
            ("cb_rs","cb_gs","cb_bs","cb_rm","cb_gm","cb_bm","cb_rh","cb_gh","cb_bh"))
    )
    if need_rgb:
        rgb = yuv_to_rgb(y, u, v)   # float32 0..255, full resolution

        # Per-channel gamma
        if p["gamma_r"] != 1.0 and p["gamma_r"] > 0:
            rgb[:,:,0] = np.power(rgb[:,:,0] / 255.0, 1.0 / p["gamma_r"]) * 255.0
        if p["gamma_g"] != 1.0 and p["gamma_g"] > 0:
            rgb[:,:,1] = np.power(rgb[:,:,1] / 255.0, 1.0 / p["gamma_g"]) * 255.0
        if p["gamma_b"] != 1.0 and p["gamma_b"] > 0:
            rgb[:,:,2] = np.power(rgb[:,:,2] / 255.0, 1.0 / p["gamma_b"]) * 255.0

        # Colorbalance : poids par zone tonale (shadows/mids/highlights)
        if any(p[k] != 0.0 for k in
               ("cb_rs","cb_gs","cb_bs","cb_rm","cb_gm","cb_bm","cb_rh","cb_gh","cb_bh")):
            L = (0.299*rgb[:,:,0] + 0.587*rgb[:,:,1] + 0.114*rgb[:,:,2]) / 255.0
            shadow_w = np.clip((1.0 - L*2.0), 0.0, 1.0)
            hi_w     = np.clip((L*2.0 - 1.0), 0.0, 1.0)
            mid_w    = 1.0 - shadow_w - hi_w
            for idx, (s_k, m_k, h_k) in enumerate(
                (("cb_rs","cb_rm","cb_rh"),
                 ("cb_gs","cb_gm","cb_gh"),
                 ("cb_bs","cb_bm","cb_bh"))):
                off = (shadow_w * p[s_k] + mid_w * p[m_k] + hi_w * p[h_k]) * 128.0
                rgb[:,:,idx] = rgb[:,:,idx] + off

        np.clip(rgb, 0, 255, out=rgb)
        y, u, v = rgb_to_yuv(rgb)

    return join_yuv(y, u, v)


# ─── Boucle principale ──────────────────────────────────────
def _ouvrir_sortie():
    path = f"/dev/shm/{{SHM_OUT}}"
    with open(path, "wb") as f:
        f.write(b"\x00" * V_TOTAL_SIZE)
    f = open(path, "r+b")
    return f, mmap.mmap(f.fileno(), V_TOTAL_SIZE)

out_f, out_shm = _ouvrir_sortie()

def _ecrire(shm, yuv, frame_index):
    slot = frame_index % V_RING_SIZE
    offset = V_HEADER_SIZE + slot * V_FRAME_SIZE
    shm[offset:offset + V_FRAME_SIZE] = yuv
    shm[0:16] = struct.pack("QQ", frame_index, time.time_ns())

frame_index = 0
start = time.time()
next_t = start
interval = 1.0 / FPS
empty_frame = b"\x10" * Y_SIZE + b"\x80" * (2 * UV_SIZE)

while True:
    if bus_error.is_set():
        bus_error.clear()
        if input_handle is not None:
            try: input_handle[1].close(); input_handle[0].close()
            except Exception: pass
        try: out_shm.close(); out_f.close()
        except Exception: pass
        time.sleep(2)
        out_f, out_shm = _ouvrir_sortie()
        continue

    now = time.time()
    wait = next_t - now
    if wait > 0: time.sleep(wait)

    with state_lock:
        params = dict(state["params"])

    in_yuv, ts_in = lire_frame_yuv(ensure_input())
    if in_yuv:
        out_yuv = appliquer_correction(in_yuv, params)
    else:
        out_yuv = empty_frame
    _ecrire(out_shm, out_yuv, frame_index)
    ts_out = time.time_ns()
    if in_yuv and ts_in:
        lat_in.push((ts_out - ts_in) / 1e6)
    frame_index += 1
    next_t = start + frame_index * interval

    if frame_index % FPS == 0:
        elapsed = time.time() - start
        with metrics_lock:
            metrics["fps"] = round(frame_index / elapsed, 1)
            metrics["frame_index"] = frame_index
            with state_lock:
                cur_input = state["input_shm"]
            metrics["inputs_latency_ms"] = {{cur_input: lat_in.avg()}} if cur_input else {{}}
