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
# MIGRÉ MXL (Phase 1, sans double-écriture) : entrée = bobimxl.Reader, sortie = bobimxl.Writer.
# Format adaptatif : WIDTH/HEIGHT/FPS/CHROMA/BIT_DEPTH sont LUS DU flow_def du flux MXL d'entrée
# (source de vérité CÔTÉ DONNÉE, écrite par le producteur → ne peut pas diverger des octets). Le
# format injecté par l'orchestrateur (POST /input) ne sert plus qu'au gating UX (ignoré ici).
#
# MODE TRANCHE (slice_mode, chantier latence sous-trame) : en genlock (verrou-entrée 1:1) et
# entrée PROGRESSIVE, la sortie corrigée est publiée BANDE PAR BANDE en suivant les tranches
# du grain SOURCE (get_slice, patch mxl-planar-slices) — la correction est PAR PIXEL (ligne-
# locale), octet-identique au plein. Entrelacé / genlock off / slice_mode off → historique.
#
# Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
# placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
# ─────────────────────────────────────────────────────────────────────────────
import time, threading, json, os, signal
from collections import deque
import numpy as np
import bobimxl
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

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True



SHM_OUT           = CONFIG.get("shm_out") or (HOSTNAME + "_cc")
_gl               = CONFIG.get("genlock", True)
GENLOCK           = _gl if isinstance(_gl, bool) else str(_gl).strip().lower() in ("1", "true", "yes", "on")
INITIAL_INPUT_SHM = (CONFIG.get("input_shm") or None)
INITIAL_PARAMS    = CONFIG.get("cc_params") or {{}}

# ── MODE TRANCHE (chantier latence sous-trame, cf. patch mxl-planar-slices) ─────────────────
# slice_mode=true → en GENLOCK (verrou-entrée 1:1, propagation d'index) et entrée PROGRESSIVE,
# le correcteur suit le grain de TÊTE de la source via get_slice (réveil à chaque commit partiel
# du producteur) et écrit la sortie corrigée BANDE PAR BANDE avec commit progressif
# (validSlices=1..N) → l'étage correcteur n'ajoute plus ~1 trame de latence, l'aval
# (multiview/TX 2110 slice) démarre sur la 1ʳᵉ bande. Convention (identique moteur/pyramide/
# udc/multiview) : k tranches ⇔ lignes image [0, k·slice_height) valides SUR LES 3 PLANS
# (Y|Cb|Cr). La correction est PAR PIXEL (ligne-locale) → la version bandée est identique
# OCTET PAR OCTET au plein (seul le glow, blur plein champ, est appliqué en fin de grain).
# Entrelacé / genlock off / hauteur sans diviseur / slice_mode absent-False → comportement
# STRICTEMENT identique à l'historique (flowDef de sortie sans slice_height).
_slm = CONFIG.get("slice_mode", False)
SLICE_MODE  = _slm if isinstance(_slm, bool) else str(_slm).strip().lower() in ("1", "true", "yes", "on")
SLICE_LINES = max(1, int(CONFIG.get("slice_lines") or 36))
# Nb de tranches CIBLE, dérivé de slice_lines (36 lignes ≈ 1080/30 → ~30 tranches) : même
# granularité TEMPORELLE que le producteur amont, adaptée à la hauteur réelle de la sortie.
_SLICE_TARGET = max(1, 1080 // SLICE_LINES)

def _cc_slice_h(oh):
    """MODE TRANCHE — slice_height de la sortie (même algo que pyramide _proxy_slice_h / udc
    _out_slice_h) : le plus petit diviseur sh de oh avec sh ≥ max(1, oh // _SLICE_TARGET)
    (~30 tranches ; 1080→36, 720→24). Aucun diviseur raisonnable (sh > oh//2, ex. hauteur
    première) → 0 = inéligible (whole-frame)."""
    lo = max(1, oh // _SLICE_TARGET)
    for sh in range(lo, oh // 2 + 1):
        if oh % sh == 0:
            return sh
    return 0

_RGBMAX = 255.0


# ─── Layout YUV : calculé à partir du format détecté/injecté ───

def _make_layout(w, h, chroma="422", bit_depth=8, fps_num=25, fps_den=1,
                 interlace_mode="progressive", frame_height=None, frame_fps_num=None):
    """Calcule tous les dérivés de format nécessaires au traitement (MXL gère le ring).
    Champ-natif : w/h = dims de GRAIN (champ si entrelacé) → on traite un champ ; les dims/cadence
    de TRAME + interlace servent à DÉCLARER la sortie entrelacée (passthrough)."""
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
    return dict(
        width=w, height=h, chroma=chroma, bit_depth=bit_depth,
        fps_num=int(fps_num), fps_den=int(fps_den), fps=max(1, round(fps_num / fps_den)),
        deep=deep, bps=bps, np_dt=np_dt,
        scale=1 << (bit_depth - 8),
        neutf=float(1 << (bit_depth - 1)),
        blackf=float(16 << (bit_depth - 8)),
        maxf=float((1 << bit_depth) - 1),
        cw=cw, ch=ch, uv_w=uv_w, uv_h=uv_h,
        y_sz=y_sz, uv_sz=uv_sz, fr_sz=fr_sz,
        interlace_mode=interlace_mode,
        frame_height=int(frame_height or h),
        frame_fps_num=int(frame_fps_num or fps_num),
    )


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
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}}, "own_latency_ms": None,
           "slice_mode": False, "plugin_version": PLUGIN_VERSION}}

# SIGBUS
bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    log("SIGBUS reçu — réouverture Reader/Writer MXL", "warning")
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


# ─── Lecture (Reader MXL) / écriture (Writer MXL) ───────────
inst        = bobimxl.Instance()   # domaine = $MXL_DOMAIN ou /dev/shm/mxl (tmpfs bind-monté)
reader      = None                 # bobimxl.Reader courant ou None
reader_name = None

def ensure_reader():
    """(Re-)crée le Reader MXL si le nom câblé a changé. None si non câblé / flux pas encore là."""
    global reader, reader_name
    with state_lock:
        wanted = state["input_shm"]
    if not wanted:
        if reader is not None:
            try: reader.close()
            except Exception: pass
            reader = None; reader_name = None
        return None
    if reader is not None and reader_name == wanted:
        return reader
    if reader is not None:
        try: reader.close()
        except Exception: pass
        reader = None; reader_name = None
    try:
        r = bobimxl.Reader(inst, wanted)   # lève si le flux n'existe pas encore
        reader = r; reader_name = wanted
        log(f"input câblé sur flux MXL {{wanted}}", "info")   # entrée qui apparaît = événement
        return reader
    except Exception:
        return None   # flux pas encore publié → on réessaiera


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

def _corriger_yuv(y, u, v, p, lyt):
    """Corps de correction HORS GLOW sur des plans numpy (Y, U, V) — ne mute pas ses entrées.
    Toutes les opérations sont LIGNE-LOCALES (LUT/float par pixel) → appelable sur la trame
    ENTIÈRE ou sur une BANDE alignée aux lignes chroma (mode tranche), résultat identique
    par construction. Le glow (blur plein champ, NON ligne-local) reste chez l'appelant."""
    neutf = lyt["neutf"]; maxf = lyt["maxf"]; np_dt = lyt["np_dt"]

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

    return y, u, v


def _glow_actif(p):
    """True si le glow (blur plein champ — NON bandable) est activé ET d'intensité > 0."""
    return bool(p.get("glow_enabled", 1.0)) and p["glow"] > 0.0


def appliquer_correction(yuv_bytes, p, lyt):
    """Applique les params de correction. Renvoie yuv bytes."""
    # IDENTITÉ → passthrough : aucune passe numpy quand rien n'est réglé.
    if _is_neutral(p):
        return yuv_bytes
    y, u, v = split_yuv(yuv_bytes, lyt)
    y, u, v = _corriger_yuv(y, u, v, p, lyt)

    # Glow EN DERNIER (sur le Y final), uniquement si activé (bouton) ET intensité > 0.
    if _glow_actif(p):
        y = _apply_glow(y, p["glow"], p["glow_thresh"], p["glow_radius"], lyt)

    return join_yuv(y, u, v)


def _plan_bande(p, lyt):
    """MODE TRANCHE — plan de traitement précalculé UNE fois par grain (hors glow, géré en fin
    de grain) : ("copy", None) identité → memcpy de bande ; ("lut", lut) seul le domaine Y est
    actif → np.take(out=) directement dans la vue du grain, ZÉRO allocation par bande ;
    ("full", None) corps complet _corriger_yuv sur la bande."""
    if (p["brightness"] == 0.0 and p["contrast"] == 1.0 and p["saturation"] == 1.0 and
            p["gamma"] == 1.0 and p["hue"] == 0.0 and
            p["gamma_r"] == 1.0 and p["gamma_g"] == 1.0 and p["gamma_b"] == 1.0 and
            all(p[k] == 0.0 for k in _CB_KEYS)):
        return ("copy", None)       # identité hors glow (glow éventuel appliqué en fin de grain)
    lut_y = _get_lut_y(p, lyt)
    if (lut_y is not None and p["saturation"] == 1.0 and p["hue"] == 0.0 and
            p["gamma_r"] == 1.0 and p["gamma_g"] == 1.0 and p["gamma_b"] == 1.0 and
            _get_lut_cb(p, lyt) is None):
        return ("lut", lut_y)
    return ("full", None)


def _corriger_bande(plan, p, lyt, y0, u0, v0, g_y, g_u, g_v, a, b):
    """MODE TRANCHE — corrige les lignes [a, b) (a et b multiples de lignes chroma entières)
    et les écrit DIRECTEMENT dans les vues du grain de sortie (zéro-copie). plan = _plan_bande
    du grain. Chemin "lut" : lut[plane[a:b]] identique par construction au lut[plane] plein,
    sans buffer intermédiaire (np.take out=). Chemin "full" : mêmes opérations que le plein
    restreintes à la bande (toutes LIGNE-LOCALES) → identique octet par octet."""
    ch = lyt["ch"]
    ca = a // ch; cb = b // ch
    mode, lut_y = plan
    if mode == "copy":
        g_y[a:b] = y0[a:b]
    elif mode == "lut":
        np.take(lut_y, y0[a:b], out=g_y[a:b])
    else:
        yb, ub, vb = _corriger_yuv(y0[a:b], u0[ca:cb], v0[ca:cb], p, lyt)
        g_y[a:b] = yb
        if cb > ca:
            g_u[ca:cb] = ub; g_v[ca:cb] = vb
        return
    if cb > ca:                       # copy/lut : chroma inchangé → memcpy de bande
        g_u[ca:cb] = u0[ca:cb]; g_v[ca:cb] = v0[ca:cb]


# ─── Boucle principale ──────────────────────────────────────
writer   = None    # bobimxl.Writer de sortie ou None
cur_lyt  = None    # layout actif (None = format inconnu)
slice_h  = 0       # MODE TRANCHE : slice_height de la sortie (0 = whole-frame)
slice_on = False   # MODE TRANCHE actif pour le format courant

def ensure_writer(lyt, slice_h=0):
    """(Re-)crée le Writer de sortie au format `lyt` (le correcteur sort au format d'entrée).
    MODE TRANCHE : slice_h > 0 → le flowDef porte slice_height, libmxl publie le grain en
    N = hauteur/slice_h tranches (commit progressif) ; 0/absent → flowDef inchangé."""
    global writer
    if writer is not None:
        try: writer.close()
        except Exception: pass
    # Champ-natif : on DÉCLARE la sortie au format TRAME + interlace (passthrough) → libmxl redonne
    # des grains-champs. On écrit le champ traité (lyt["height"] lignes) à l'index du grain d'entrée
    # (parité de champ préservée). Progressif : frame_*==grain, interlace=progressive → inchangé.
    writer = bobimxl.Writer(inst, SHM_OUT, lyt["width"], lyt["frame_height"], lyt["chroma"],
                            lyt["bit_depth"], lyt["frame_fps_num"], lyt["fps_den"],
                            index_mode=("tai" if GENLOCK else "free"),
                            interlace=lyt["interlace_mode"],
                            **({{"slice_height": int(slice_h)}} if slice_h else {{}}))

frame_index = 0
last_in_idx = -1
start       = time.time()
next_t      = start
last_black  = start
_fps_last_idx = 0          # fps en fenêtre glissante (delta depuis le dernier report)
_fps_last_t   = start

while True:
    if bus_error.is_set():
        bus_error.clear()
        if reader is not None:
            try: reader.close()
            except Exception: pass
        if writer is not None:
            try: writer.close()
            except Exception: pass
        reader = reader_name = writer = None
        cur_lyt = None
        time.sleep(2)
        last_in_idx = -1
        continue

    r = ensure_reader()

    # ─── Format LU DU flow_def du flux (source de vérité côté donnée) ──
    new_lyt = None
    if r is not None:
        f = r.format()
        if f:
            new_lyt = _make_layout(f["width"], f["height"], f["chroma"],
                                   f["bit_depth"], f["fps_num"], f["fps_den"],
                                   f.get("interlace_mode", "progressive"),
                                   f.get("frame_height"), f.get("frame_fps_num"))
    with state_lock:
        in_name = state["input_shm"]

    if new_lyt is None:
        # Flux pas encore publié / format indispo → attendre (le hint orchestrateur n'est PAS
        # utilisé pour décoder ; il ne sert qu'au gating UX côté contrôleur).
        time.sleep(0.05)
        continue

    # (Re)configurer la sortie si le format a changé
    if cur_lyt is None or cur_lyt["fr_sz"] != new_lyt["fr_sz"]:
        cur_lyt = new_lyt
        # MODE TRANCHE : éligibilité par format — genlock (verrou 1:1) + entrée PROGRESSIVE +
        # hauteur découpable (_cc_slice_h). Sinon slice_h = 0 → chemin historique STRICTEMENT
        # inchangé (flowDef de sortie sans slice_height), repli loggé.
        slice_h = 0
        if SLICE_MODE:
            _il = str(cur_lyt.get("interlace_mode") or "progressive").startswith("interlaced")
            if not GENLOCK:
                log("slice_mode demandé mais genlock off → repli whole-frame", "warning")
            elif _il:
                log("slice_mode demandé mais entrée ENTRELACÉE → repli whole-frame", "warning")
            else:
                slice_h = _cc_slice_h(cur_lyt["height"])
                if not slice_h:
                    log(f"slice_mode demandé mais hauteur {{cur_lyt['height']}} sans diviseur "
                        "raisonnable → repli whole-frame", "warning")
        slice_on = slice_h > 0
        with metrics_lock:
            metrics["slice_mode"] = slice_on
        ensure_writer(cur_lyt, slice_h)
        last_in_idx = -1
        frame_index = 0
        start = time.time(); next_t = start; last_black = start
        # Trame noire de repli CACHÉE : calculée UNE fois au changement de format.
        empty_frame = (b"\x10" * cur_lyt["y_sz"] +
                       b"\x80" * (2 * cur_lyt["uv_sz"]))
        # Changement de format = ÉVÉNEMENT (visible au niveau par défaut).
        log(f"format: {{cur_lyt['width']}}x{{cur_lyt['height']}} chroma={{cur_lyt['chroma']}} {{cur_lyt['bit_depth']}}b"
            + (f" [tranches sh={{slice_h}}]" if slice_on else ""), "info")

    fps      = cur_lyt["fps"]
    interval = 1.0 / fps

    # ─── Lecture du grain d'entrée ────────────────────────────
    got = None
    try:
        if slice_on:
            # MODE TRANCHE : suivre le grain de TÊTE (peut être EN COURS d'écriture).
            h_idx = r.head_index()
            if h_idx != bobimxl.MXL_UNDEFINED_INDEX and h_idx == last_in_idx:
                time.sleep(0.002); continue     # même grain (source figée) — comme idx==last
            if h_idx != bobimxl.MXL_UNDEFINED_INDEX:
                # 1ʳᵉ tranche du grain de tête ; pas encore là (tête à peine réclamée) ou flux
                # sans le patch slices → repli get_latest (grain complet, boucle dégénérée).
                got = r.get_slice(h_idx, 1, timeout_ns=2_000_000)
                if got is None:
                    got = r.get_latest()
            # h_idx indéfini → got=None : pas d'entrée → noir GENLOCK (comme l'historique)
        else:
            got = r.get_latest()
    except Exception:
        # flux disparu (producteur redéployé) → re-créer le Reader au prochain tour
        try: reader.close()
        except Exception: pass
        # GC ENTRE close et reopen (parade générique du piège des générations, cf. pyramide/
        # moteur tx_reopen_if_stale) : sans GC le flux périmé reste résolvable par nom et la
        # réouverture (même nom via ensure_reader) retombe sur L'ORPHELIN → gel permanent.
        try: inst.garbage_collect()
        except Exception: pass
        reader = reader_name = None
        time.sleep(0.05); continue

    if GENLOCK:
        if got is None:
            now = time.time()
            if now - last_black >= interval:   # pas d'entrée → noir sur la grille
                _gi, gi_b, vw_b = writer.open_grain()
                vw_b[:len(empty_frame)] = np.frombuffer(empty_frame, dtype=np.uint8)
                writer.commit(gi_b)
                last_black = now
            time.sleep(0.002); continue
        idx_in = got[0]
        if idx_in == last_in_idx:
            time.sleep(0.002); continue
        last_in_idx = idx_in; last_black = time.time()
    else:
        now  = time.time()
        wait = next_t - now
        if wait > 0: time.sleep(wait)
        idx_in = got[0] if got else None

    with state_lock:
        params = dict(state["params"])

    ts_cycle_start = time.time_ns()   # base own_latency
    wait_ns = 0   # cumul des ATTENTES get_slice (mode tranche) — EXCLUES de own_latency_ms
    if slice_on and got is not None:
        # ─── MODE TRANCHE : correction BANDE PAR BANDE, écrite en zéro-copie dans le grain ───
        transit_ms = (bobimxl.now_tai() - r.last_write_time()) / 1e6   # TRANSIT (TAI), à la lecture
        gi_s = got[1]
        src_h = cur_lyt["height"]; s_ch = cur_lyt["ch"]
        ny = cur_lyt["width"] * src_h
        nu = cur_lyt["uv_w"] * cur_lyt["uv_h"]
        np_dt = cur_lyt["np_dt"]
        try:
            # Vues ZÉRO-COPIE sur le payload source (PAS de bytes() copie : on ne lit jamais
            # au-delà des tranches valides — attente ciblée plus bas ; le handler SIGBUS
            # couvre la recréation du flux amont, comme pour le chemin historique).
            arr = got[2][:cur_lyt["fr_sz"]].view(np_dt)
            y0 = arr[:ny].reshape(src_h, cur_lyt["width"])
            u0 = arr[ny:ny + nu].reshape(cur_lyt["uv_h"], cur_lyt["uv_w"])
            v0 = arr[ny + nu:ny + 2 * nu].reshape(cur_lyt["uv_h"], cur_lyt["uv_w"])
        except Exception:
            time.sleep(0.002); continue
        # Grain de sortie à l'index SOURCE (genlock par PROPAGATION, inchangé) + vues par plan.
        _gidx, gi_o, vw_o = writer.open_grain(index=idx_in)
        ysz = cur_lyt["y_sz"]; usz = cur_lyt["uv_sz"]
        g_y = vw_o[:ysz].view(np_dt).reshape(src_h, cur_lyt["width"])
        g_u = vw_o[ysz:ysz + usz].view(np_dt).reshape(cur_lyt["uv_h"], cur_lyt["uv_w"])
        g_v = vw_o[ysz + usz:ysz + 2 * usz].view(np_dt).reshape(cur_lyt["uv_h"], cur_lyt["uv_w"])
        plan = _plan_bande(params, cur_lyt)
        # Glow actif → PAS de commit progressif (le blur est plein champ, appliqué en fin de
        # grain : publier des bandes pré-glow exposerait un rendu différent du final).
        glow_on = _glow_actif(params)
        total = max(1, int(gi_s.totalSlices or 1))
        islh  = max(1, src_h // total)   # lignes source par tranche (tranches égales)
        valid = max(1, int(gi_s.validSlices or 1))
        # Budget d'attente TOTAL ≈ 1,5 période de trame : une source en retard ne bloque
        # jamais la sortie au-delà d'une demi-trame après le nominal.
        deadl_ns = time.monotonic_ns() + int(1.5e9 * cur_lyt["fps_den"]
                                             / max(1, cur_lyt["fps_num"]))
        written = 0; k_done = 0
        try:
            for j in range(1, total + 1):
                if j > valid:
                    left = deadl_ns - time.monotonic_ns()
                    _w0 = time.monotonic_ns()
                    g = (r.get_slice(idx_in, j, timeout_ns=max(1, left))
                         if left > 0 else None)
                    wait_ns += time.monotonic_ns() - _w0
                    if g is not None:
                        valid = max(j, int(g[1].validSlices or j))
                    else:
                        # Budget épuisé / producteur en retard → REPLI : compléter la sortie
                        # depuis le grain COMPLET précédent (idx-1) si disponible (léger
                        # tearing d'UNE image), sinon les lignes déjà écrites restent. Le
                        # commit FINAL est garanti par le finally.
                        gp = r.get(idx_in - 1, timeout_ns=2_000_000) if idx_in > 0 else None
                        if gp is not None:
                            try:
                                arrp = gp[2][:cur_lyt["fr_sz"]].view(np_dt)
                                y0 = arrp[:ny].reshape(src_h, cur_lyt["width"])
                                u0 = arrp[ny:ny + nu].reshape(cur_lyt["uv_h"], cur_lyt["uv_w"])
                                v0 = arrp[ny + nu:ny + 2 * nu].reshape(cur_lyt["uv_h"],
                                                                       cur_lyt["uv_w"])
                                _corriger_bande(plan, params, cur_lyt, y0, u0, v0,
                                                g_y, g_u, g_v, written, src_h)
                                written = src_h
                            except Exception:
                                pass
                        break
                # Tranche source j dispo : lignes [0, sr) valides sur les 3 plans → la sortie
                # est en GÉOMÉTRIE IDENTIQUE : delta de lignes [written, b) directement
                # corrigeable, borné aux lignes CHROMA source entières (b multiple de ch).
                sr = min(src_h, j * islh)
                b = sr - (sr % s_ch)
                if b > written:
                    _corriger_bande(plan, params, cur_lyt, y0, u0, v0,
                                    g_y, g_u, g_v, written, b)
                    written = b
                if j < total and not glow_on:
                    k = written // slice_h
                    if k > k_done:   # commit progressif (réveille l'aval), jamais en arrière
                        k_done = k
                        writer.commit(gi_o, valid_slices=k)
        finally:
            # Glow EN DERNIER (blur plein champ sur le Y final du grain — même entrée que le
            # chemin plein : le Y corrigé — donc résultat identique), puis commit FINAL
            # TOUJOURS (un grain laissé partiel ne serait jamais lisible par un consommateur
            # whole-frame).
            if glow_on:
                try:
                    g_y[:] = _apply_glow(g_y, params["glow"], params["glow_thresh"],
                                         params["glow_radius"], cur_lyt)
                except Exception: pass
            try: writer.commit(gi_o, valid_slices=None)
            except Exception: pass
        lat_in.push(transit_ms)
    else:
        if got is not None:
            in_yuv  = bytes(got[2])                       # vue numpy du grain → octets planar
            out_yuv = appliquer_correction(in_yuv, params, cur_lyt)
            lat_in.push((bobimxl.now_tai() - r.last_write_time()) / 1e6)   # TRANSIT (TAI)
        else:
            out_yuv = empty_frame

        # Écriture du grain de sortie — genlock par PROPAGATION : même index que l'entrée.
        out_idx = idx_in if (GENLOCK and got is not None) else None
        _gidx, gi_o, vw_o = writer.open_grain(index=out_idx)
        vw_o[:len(out_yuv)] = np.frombuffer(out_yuv, dtype=np.uint8)
        writer.commit(gi_o)
    ts_out = time.time_ns()
    # own = traitement PROPRE (correction) SANS les attentes get_slice (suivi du fil ≈ période
    # de trame en mode tranche) — même contrat que pyramide/udc : l'orchestrateur y lit la
    # SATURATION du nœud, pas le suivi.
    own_lat.push((ts_out - ts_cycle_start - wait_ns) / 1e6)
    frame_index += 1
    if not GENLOCK:
        next_t = start + frame_index * interval

    if frame_index % max(1, fps) == 0:
        _now = time.time(); _dt = _now - _fps_last_t
        with metrics_lock:
            if _dt > 0 and frame_index >= _fps_last_idx:
                metrics["fps"] = round((frame_index - _fps_last_idx) / _dt, 1)
            _fps_last_idx = frame_index; _fps_last_t = _now
            metrics["frame_index"] = frame_index
            metrics["inputs_latency_ms"] = {{in_name: lat_in.avg()}} if in_name else {{}}
            metrics["own_latency_ms"] = own_lat.avg()
