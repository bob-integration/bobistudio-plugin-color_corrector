# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Color corrector plugin — corrige un flux vidéo YUV du pipeline MXL (luma/
# chroma + per-channel gamma + color balance), réglable À CHAUD via :8082.
#   Entrée  : input_shm (câblable à chaud, mode hot-wire)
#   Sortie  : {hostname}_cc
#
# Format adaptatif : WIDTH/HEIGHT/FPS/CHROMA/BIT_DEPTH sont détectés à la
# connexion du câble (format injecté par l'orchestrateur via POST /input, ou
# déduit de la taille du shm d'entrée avec heuristique 16:9).
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import mmap, struct, time, threading, json, os, signal, math
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

lat_in  = RollingMs()   # TRANSIT (ts_read − ts_in producteur) = arrivée
own_lat = RollingMs()   # traitement PROPRE du nœud (ts_out − ts_read) → own_latency_ms

# ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

SHM_OUT           = CONFIG.get("shm_out") or (HOSTNAME + "_cc")
_gl               = CONFIG.get("genlock", True)
GENLOCK           = _gl if isinstance(_gl, bool) else str(_gl).strip().lower() in ("1", "true", "yes", "on")
INITIAL_INPUT_SHM = (CONFIG.get("input_shm") or None)
INITIAL_PARAMS    = CONFIG.get("cc_params") or {{}}

V_HEADER_SIZE = 64
V_RING_SIZE   = 10
_RGBMAX = 255.0


# ─── Layout YUV : calculé à partir du format détecté/injecté ───

def _make_layout(w, h, chroma="422", bit_depth=8, fps=25):
    """Calcule tous les dérivés de format nécessaires au traitement."""
    w -= w % 2; h -= h % 2
    deep  = bit_depth >= 10
    bps   = 2 if deep else 1
    np_dt = np.uint16 if deep else np.uint8
    cw    = {{"420": 2, "422": 2, "444": 1}}.get(chroma, 2)
    ch    = {{"420": 2, "422": 1, "444": 1}}.get(chroma, 1)
    uv_w  = w // cw;  uv_h = h // ch
    y_sz  = w * h * bps
    uv_sz = uv_w * uv_h * bps
    fr_sz = y_sz + 2 * uv_sz
    total = V_HEADER_SIZE + V_RING_SIZE * fr_sz
    return dict(
        width=w, height=h, chroma=chroma, bit_depth=bit_depth, fps=fps,
        deep=deep, bps=bps, np_dt=np_dt,
        scale=1 << (bit_depth - 8),
        neutf=float(1 << (bit_depth - 1)),
        blackf=float(16 << (bit_depth - 8)),
        maxf=float((1 << bit_depth) - 1),
        cw=cw, ch=ch, uv_w=uv_w, uv_h=uv_h,
        y_sz=y_sz, uv_sz=uv_sz, fr_sz=fr_sz, total=total,
    )

def _detect_fmt(path):
    """Déduit W×H depuis la taille du fichier shm (ring=10 header=64).
    Essaie d'abord YUV422 8-bit puis YUV420 8-bit, ratio 16:9."""
    try:
        sz = os.path.getsize(path)
    except OSError:
        return None
    fs = (sz - V_HEADER_SIZE) // V_RING_SIZE
    if fs <= 0:
        return None
    # YUV422 8bit : frame = w*h*2 ; ratio 16:9 → fs = 288*k²
    k2 = fs / 288.0
    k  = math.isqrt(int(k2))
    for kk in (k, k + 1):
        w, h = kk * 16, kk * 9
        if w > 0 and w * h * 2 == fs:
            return {{"width": w, "height": h, "chroma": "422", "bit_depth": 8}}
    # YUV420 8bit : frame = w*h*3//2 ; ratio 16:9 → fs = 216*k²
    k2 = fs / 216.0
    k  = math.isqrt(int(k2))
    for kk in (k, k + 1):
        w, h = kk * 16, kk * 9
        if w > 0 and w * h + 2 * (w // 2) * (h // 2) == fs:
            return {{"width": w, "height": h, "chroma": "420", "bit_depth": 8}}
    return None


# ─── Paramètres colorimétriques ─────────────────────────────
DEFAULT_PARAMS = {{
    "brightness": 0.0,   # -1..1
    "contrast":   1.0,   # 0..2
    "saturation": 1.0,   # 0..3
    "gamma":      1.0,   # 0.1..10
    "hue":        0.0,   # deg, -180..180
    "gamma_r":    1.0,   # 0.1..10
    "gamma_g":    1.0,
    "gamma_b":    1.0,
    # Glow / bloom : diffusion lumineuse des hautes lumières (luma only) — APPLIQUÉ EN DERNIER.
    "glow_enabled": 1.0, # bouton on/off (0 = glow désactivé même si intensité > 0)
    "glow":        0.0,  # 0..2 intensité (0 = désactivé)
    "glow_thresh": 0.7,  # 0..1 seuil hautes lumières (fraction de la plage)
    "glow_radius": 8.0,  # 1..64 rayon de diffusion (px, plein écran)
    # Colorbalance : -1..1 par canal × 3 zones (shadows/mids/highlights)
    "cb_rs": 0.0, "cb_gs": 0.0, "cb_bs": 0.0,
    "cb_rm": 0.0, "cb_gm": 0.0, "cb_bm": 0.0,
    "cb_rh": 0.0, "cb_gh": 0.0, "cb_bh": 0.0,
}}

def _norm_params(d):
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
    "fmt":       None,    # dict injecté par /input ou None (→ auto-détection)
}}

# Métriques
metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}}, "own_latency_ms": None, "plugin_version": PLUGIN_VERSION}}

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
            shm = (body.get("shm") or "").strip() or None
            fmt = body.get("format")
            with state_lock:
                state["input_shm"] = shm
                if fmt and fmt.get("width") and fmt.get("height"):
                    state["fmt"] = fmt
                elif shm is None:
                    state["fmt"] = None
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
        if os.path.getsize(path) <= V_HEADER_SIZE: return None
        f = open(path, "r+b")
        sz = os.path.getsize(path)
        shm = mmap.mmap(f.fileno(), sz, prot=mmap.PROT_READ)
        _ = shm[0:16]
        input_handle = (f, shm, wanted)
        print(f"input câblé sur {{path}}")
        return shm
    except Exception as e:
        print(f"input ({{wanted}}) indisponible : {{e}}")
        return None

def lire_frame_yuv(shm, lyt):
    """Renvoie (yuv_bytes, ts_in_ns) ou (None, None)."""
    if shm is None: return None, None
    try:
        frame_index, ts_in = struct.unpack("QQ", shm[0:16])
        if frame_index == 0: return None, None
        slot   = frame_index % lyt["ring_r"]
        offset = V_HEADER_SIZE + slot * lyt["fr_sz"]
        data   = bytes(shm[offset:offset + lyt["fr_sz"]])
        if len(data) < lyt["fr_sz"]:
            return None, None   # lecture TRONQUÉE (ring/format mismatch) → trame ignorée, JAMAIS de crash
        return data, ts_in
    except Exception:
        return None, None


# ─── Color correction (numpy) ───────────────────────────────
def split_yuv(yuv_bytes, lyt):
    y_sz = lyt["y_sz"]; uv_sz = lyt["uv_sz"]
    np_dt = lyt["np_dt"]; h = lyt["height"]; w = lyt["width"]
    uv_h = lyt["uv_h"]; uv_w = lyt["uv_w"]
    y = np.frombuffer(yuv_bytes[:y_sz],           dtype=np_dt).reshape(h, w).copy()
    u = np.frombuffer(yuv_bytes[y_sz:y_sz+uv_sz], dtype=np_dt).reshape(uv_h, uv_w).copy()
    v = np.frombuffer(yuv_bytes[y_sz+uv_sz:],     dtype=np_dt).reshape(uv_h, uv_w).copy()
    return y, u, v

def join_yuv(y, u, v):
    return y.tobytes() + u.tobytes() + v.tobytes()

def yuv_to_rgb(y, u, v, lyt):
    """Y(H,W) + U,V → RGB float32 (H,W,3) en 0..255."""
    sc = lyt["scale"]; ch = lyt["ch"]; cw = lyt["cw"]
    yf = y.astype(np.float32) / sc
    uf = u.repeat(ch, axis=0).repeat(cw, axis=1).astype(np.float32) / sc
    vf = v.repeat(ch, axis=0).repeat(cw, axis=1).astype(np.float32) / sc
    c = yf - 16.0; d = uf - 128.0; e = vf - 128.0
    r = 1.164*c              + 1.596*e
    g = 1.164*c - 0.392*d    - 0.813*e
    b = 1.164*c + 2.017*d
    return np.stack([np.clip(r,0,255), np.clip(g,0,255), np.clip(b,0,255)], axis=2)

def rgb_to_yuv(rgb, lyt):
    """RGB float32 (H,W,3) 0..255 → (Y, U, V) à la profondeur cible."""
    sc = lyt["scale"]; ch = lyt["ch"]; cw = lyt["cw"]
    maxf = lyt["maxf"]; np_dt = lyt["np_dt"]
    r = rgb[:,:,0]; g = rgb[:,:,1]; b = rgb[:,:,2]
    y = ( 0.257*r + 0.504*g + 0.098*b + 16.0)  * sc
    u = (-0.148*r - 0.291*g + 0.439*b + 128.0) * sc
    v = ( 0.439*r - 0.368*g - 0.071*b + 128.0) * sc
    y_out = np.clip(y,            0, maxf).astype(np_dt)
    u_out = np.clip(u[::ch,::cw], 0, maxf).astype(np_dt)
    v_out = np.clip(v[::ch,::cw], 0, maxf).astype(np_dt)
    return y_out, u_out, v_out

_GLOW_DS = 4

def _box_blur(img, r):
    r = int(r)
    if r < 1: return img
    k = 2 * r + 1
    pad = np.pad(img, ((0, 0), (r + 1, r)), mode="constant")
    cs  = np.cumsum(pad, axis=1, dtype=np.float32)
    img = (cs[:, k:] - cs[:, :-k]) / k
    pad = np.pad(img, ((r + 1, r), (0, 0)), mode="constant")
    cs  = np.cumsum(pad, axis=0, dtype=np.float32)
    return (cs[k:, :] - cs[:-k, :]) / k

def _apply_glow(y, intensity, thresh, radius, lyt):
    H, W = y.shape
    blackf = lyt["blackf"]; maxf = lyt["maxf"]; np_dt = lyt["np_dt"]
    thr = blackf + max(0.0, min(1.0, thresh)) * (maxf - blackf)
    ds  = _GLOW_DS; r = max(1, int(radius) // ds)
    small = y[::ds, ::ds].astype(np.float32) - thr
    np.maximum(small, 0.0, out=small)
    small = _box_blur(small, r); small = _box_blur(small, r)
    up = (small * intensity).repeat(ds, 0).repeat(ds, 1)[:H, :W]
    return np.clip(y.astype(np.float32) + up, 0, maxf).astype(np_dt)

_CB_KEYS = ("cb_rs", "cb_gs", "cb_bs", "cb_rm", "cb_gm", "cb_bm", "cb_rh", "cb_gh", "cb_bh")

def _is_neutral(p):
    """True si AUCUNE correction n'est active (identité) → passthrough sans aucune passe numpy."""
    return (p["brightness"] == 0.0 and p["contrast"] == 1.0 and p["saturation"] == 1.0 and
            p["gamma"] == 1.0 and p["hue"] == 0.0 and
            p["gamma_r"] == 1.0 and p["gamma_g"] == 1.0 and p["gamma_b"] == 1.0 and
            not (p.get("glow_enabled", 1.0) and p["glow"] > 0.0) and
            all(p[k] == 0.0 for k in _CB_KEYS))

# LUT Y (luminosité+contraste+gamma) cachée : recalculée SEULEMENT au changement de params.
# Une op par-pixel d'un seul canal = une table → 1 gather au lieu de passes float + np.power/pixel.
_lut_y_cache = {{"key": None, "lut": None}}

def _get_lut_y(p, lyt):
    """LUT Y appliquant luminosité→contraste→gamma (bit-exact vs l'ancien float→quantize), ou None
    si le domaine Y est l'identité (rien à faire)."""
    if p["brightness"] == 0.0 and p["contrast"] == 1.0 and p["gamma"] == 1.0:
        return None
    neutf = lyt["neutf"]; maxf = lyt["maxf"]; np_dt = lyt["np_dt"]
    key = (p["brightness"], p["contrast"], p["gamma"], maxf, neutf)
    c = _lut_y_cache
    if c["key"] == key:
        return c["lut"]
    vf = np.arange(int(maxf) + 1, dtype=np.float32)
    if p["brightness"] != 0.0:
        vf = vf + p["brightness"] * neutf
    if p["contrast"] != 1.0:
        vf = (vf - neutf) * p["contrast"] + neutf
    if p["gamma"] != 1.0 and p["gamma"] > 0:
        yn = np.clip(vf, 0, maxf) / maxf
        vf = np.power(yn, 1.0 / p["gamma"]) * maxf
    c["key"] = key
    c["lut"] = np.clip(vf, 0, maxf).astype(np_dt)
    return c["lut"]

# LUTs de COLOR BALANCE en domaine YUV (cachées). Le balance ajoute un offset RGB pondéré par la
# luma (zones shadow/mid/high) ; la luma = Y (le chroma s'annule dans la conversion) → l'offset
# YUV (dy/du/dv) est une FONCTION DE Y SEUL → 3 LUTs au lieu du roundtrip yuv↔rgb (qui en plus
# distordait l'image, ~Y±12). Le gamma R/G/B (non-linéaire) garde son roundtrip RGB, lui.
_lut_cb_cache = {{"key": None, "luts": None}}

def _get_lut_cb(p, lyt):
    """(dy,du,dv) float32 indexées par Y, ou None si aucun cb actif."""
    cbv = tuple(p[k] for k in _CB_KEYS)
    if not any(cbv):
        return None
    sc = lyt["scale"]; maxf = lyt["maxf"]
    key = (cbv, sc, maxf)
    c = _lut_cb_cache
    if c["key"] == key:
        return c["luts"]
    v = np.arange(int(maxf) + 1, dtype=np.float32)
    L = np.clip(1.164 * (v / sc - 16.0) / 255.0, 0.0, 1.0)   # luma normalisée (= L de l'ancien rgb)
    sw = np.clip(1.0 - 2.0 * L, 0.0, 1.0); hw = np.clip(2.0 * L - 1.0, 0.0, 1.0); mw = 1.0 - sw - hw
    dr = (sw * p["cb_rs"] + mw * p["cb_rm"] + hw * p["cb_rh"]) * 128.0
    dg = (sw * p["cb_gs"] + mw * p["cb_gm"] + hw * p["cb_gh"]) * 128.0
    db = (sw * p["cb_bs"] + mw * p["cb_bm"] + hw * p["cb_bh"]) * 128.0
    dy = ( 0.257 * dr + 0.504 * dg + 0.098 * db) * sc
    du = (-0.148 * dr - 0.291 * dg + 0.439 * db) * sc
    dv = ( 0.439 * dr - 0.368 * dg - 0.071 * db) * sc
    c["key"] = key
    c["luts"] = (dy, du, dv)
    return c["luts"]

def appliquer_correction(yuv_bytes, p, lyt):
    """Applique les params de correction. Renvoie yuv bytes."""
    # IDENTITÉ → passthrough : aucune passe numpy quand rien n'est réglé.
    if _is_neutral(p):
        return yuv_bytes
    neutf = lyt["neutf"]; maxf = lyt["maxf"]; np_dt = lyt["np_dt"]
    y, u, v = split_yuv(yuv_bytes, lyt)

    # Domaine Y (luminosité/contraste/gamma) : un seul gather via LUT cachée (plus de float ni np.power).
    lut_y = _get_lut_y(p, lyt)
    if lut_y is not None:
        y = lut_y[y]

    # Chroma (saturation/teinte) : float sur U/V (petits) UNIQUEMENT si actif.
    if p["saturation"] != 1.0 or p["hue"] != 0.0:
        uf = u.astype(np.float32) - neutf
        vf = v.astype(np.float32) - neutf
        if p["saturation"] != 1.0:
            uf = uf * p["saturation"]; vf = vf * p["saturation"]
        if p["hue"] != 0.0:
            rad = p["hue"] * np.pi / 180.0
            cs, sn = np.cos(rad), np.sin(rad)
            uf, vf = uf * cs - vf * sn, uf * sn + vf * cs
        u = np.clip(uf + neutf, 0, maxf).astype(np_dt)
        v = np.clip(vf + neutf, 0, maxf).astype(np_dt)

    # Gamma par-canal R/G/B = NON-linéaire → roundtrip RGB obligatoire (+ color balance fait dedans).
    need_rgb = p["gamma_r"] != 1.0 or p["gamma_g"] != 1.0 or p["gamma_b"] != 1.0
    if need_rgb:
        rgb = yuv_to_rgb(y, u, v, lyt)
        if p["gamma_r"] != 1.0 and p["gamma_r"] > 0:
            rgb[:,:,0] = np.power(rgb[:,:,0] / 255.0, 1.0 / p["gamma_r"]) * 255.0
        if p["gamma_g"] != 1.0 and p["gamma_g"] > 0:
            rgb[:,:,1] = np.power(rgb[:,:,1] / 255.0, 1.0 / p["gamma_g"]) * 255.0
        if p["gamma_b"] != 1.0 and p["gamma_b"] > 0:
            rgb[:,:,2] = np.power(rgb[:,:,2] / 255.0, 1.0 / p["gamma_b"]) * 255.0
        if any(p[k] != 0.0 for k in _CB_KEYS):
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
        y, u, v = rgb_to_yuv(rgb, lyt)
    else:
        # Color balance SANS gamma RGB → domaine YUV via LUTs(Y) (pas de roundtrip RGB).
        cb_luts = _get_lut_cb(p, lyt)
        if cb_luts is not None:
            dy, du, dv = cb_luts
            ys = y[::lyt["ch"], ::lyt["cw"]]          # Y aux positions chroma
            y = np.clip(y.astype(np.float32) + dy[y],  0, maxf).astype(np_dt)
            u = np.clip(u.astype(np.float32) + du[ys], 0, maxf).astype(np_dt)
            v = np.clip(v.astype(np.float32) + dv[ys], 0, maxf).astype(np_dt)

    # Glow EN DERNIER (sur le Y final), uniquement si activé (bouton) ET intensité > 0.
    if p.get("glow_enabled", 1.0) and p["glow"] > 0.0:
        y = _apply_glow(y, p["glow"], p["glow_thresh"], p["glow_radius"], lyt)

    return join_yuv(y, u, v)


# ─── Boucle principale ──────────────────────────────────────
out_f   = None
out_shm = None
cur_lyt = None    # layout actif (None = pas encore de format connu)

def _ouvrir_sortie(total_size):
    global out_f, out_shm
    path = f"/dev/shm/{{SHM_OUT}}"
    if out_shm is not None:
        try: out_shm.close()
        except Exception: pass
    if out_f is not None:
        try: out_f.close()
        except Exception: pass
    with open(path, "wb") as f:
        f.write(b"\x00" * total_size)
    out_f   = open(path, "r+b")
    out_shm = mmap.mmap(out_f.fileno(), total_size)

def _ecrire(yuv, frame_index, lyt):
    slot   = frame_index % V_RING_SIZE
    offset = V_HEADER_SIZE + slot * lyt["fr_sz"]
    out_shm[offset:offset + lyt["fr_sz"]] = yuv
    out_shm[0:16] = struct.pack("QQ", frame_index, time.time_ns())

def _in_index(shm):
    if shm is None: return 0
    try: return struct.unpack_from("Q", shm, 0)[0]
    except Exception: return 0

frame_index = 0
last_in_idx = 0
start       = time.time()
next_t      = start
last_black  = start

while True:
    if bus_error.is_set():
        bus_error.clear()
        if input_handle is not None:
            try: input_handle[1].close(); input_handle[0].close()
            except Exception: pass
        if out_shm is not None:
            try: out_shm.close(); out_f.close()
            except Exception: pass
            out_f = out_shm = None
        cur_lyt = None
        time.sleep(2)
        last_in_idx = 0
        continue

    shm_in = ensure_input()

    # ─── Résolution du format ────────────────────────────────
    with state_lock:
        fmt_hint = state["fmt"]
        in_name  = state["input_shm"]
    new_lyt = None
    if fmt_hint and fmt_hint.get("width") and fmt_hint.get("height"):
        new_lyt = _make_layout(
            fmt_hint["width"], fmt_hint["height"],
            fmt_hint.get("chroma", "422"),
            fmt_hint.get("bit_depth", 8),
            fmt_hint.get("fps", 25),
        )
    elif shm_in is not None and in_name:
        detected = _detect_fmt(f"/dev/shm/{{in_name}}")
        if detected:
            new_lyt = _make_layout(detected["width"], detected["height"],
                                   detected.get("chroma", "422"),
                                   detected.get("bit_depth", 8))

    if new_lyt is None:
        # Format inconnu : attendre que l'entrée soit disponible
        time.sleep(0.05)
        continue

    # Reconfigurer le shm de sortie si le format a changé
    if cur_lyt is None or cur_lyt["total"] != new_lyt["total"]:
        cur_lyt = new_lyt
        # ring_r de l'ENTRÉE = DÉRIVÉ de la taille RÉELLE du shm producteur (PAS le ring de sortie).
        # Un producteur en ring 8 (RX 2110_io) lu en supposant ring 10 → l'offset des slots 8/9
        # déborde le shm → lecture tronquée → reshape crash. cf. mxl-consumer-format-contract.
        try:
            _in_sz = os.path.getsize(f"/dev/shm/{{in_name}}")
            cur_lyt["ring_r"] = max(1, (_in_sz - V_HEADER_SIZE) // cur_lyt["fr_sz"])
        except Exception:
            cur_lyt["ring_r"] = V_RING_SIZE
        _ouvrir_sortie(cur_lyt["total"])
        last_in_idx = 0
        frame_index = 0
        start = time.time(); next_t = start; last_black = start
        # Trame noire de repli CACHÉE (4 Mo) : calculée UNE fois au changement de format au lieu
        # d'être reconstruite à chaque itération de la boucle (gaspillage CPU dans l'attente d'entrée).
        empty_frame = (b"\x10" * cur_lyt["y_sz"] +
                       b"\x80" * (2 * cur_lyt["uv_sz"]))
        print(f"format: {{cur_lyt['width']}}x{{cur_lyt['height']}} chroma={{cur_lyt['chroma']}}")

    fps      = cur_lyt["fps"]
    interval = 1.0 / fps

    if GENLOCK:
        idx_in = _in_index(shm_in)
        if shm_in is None:
            now = time.time()
            if now - last_black >= interval:
                _ecrire(empty_frame, frame_index, cur_lyt)
                frame_index += 1; last_black = now
            time.sleep(0.002); continue
        if idx_in == 0 or idx_in == last_in_idx:
            time.sleep(0.002); continue
        last_in_idx = idx_in; last_black = time.time()
    else:
        now  = time.time()
        wait = next_t - now
        if wait > 0: time.sleep(wait)

    with state_lock:
        params = dict(state["params"])

    ts_cycle_start = time.time_ns()   # instant de LECTURE (après pacing) → base transit/own
    in_yuv, ts_in = lire_frame_yuv(shm_in, cur_lyt)
    if in_yuv:
        out_yuv = appliquer_correction(in_yuv, params, cur_lyt)
    else:
        out_yuv = empty_frame
    _ecrire(out_yuv, frame_index, cur_lyt)
    ts_out = time.time_ns()
    if in_yuv and ts_in:
        lat_in.push((ts_cycle_start - ts_in) / 1e6)   # TRANSIT (arrivée) = âge à la lecture
    own_lat.push((ts_out - ts_cycle_start) / 1e6)      # traitement PROPRE (correction)
    frame_index += 1
    if not GENLOCK:
        next_t = start + frame_index * interval

    if frame_index % max(1, fps) == 0:
        elapsed = time.time() - start
        with metrics_lock:
            metrics["fps"] = round(frame_index / elapsed, 1)
            metrics["frame_index"] = frame_index
            metrics["inputs_latency_ms"] = {{in_name: lat_in.avg()}} if in_name else {{}}
            metrics["own_latency_ms"] = own_lat.avg()
