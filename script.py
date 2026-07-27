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
                    "log_level": LOG_LEVEL,   # lisible en condition de macro
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
        elif self.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            ok = set_log_level(body.get("level") or body.get("log_level"))
            self._reply(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})
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

# ─── Noyau C FUSIONNÉ de la branche « full » (roundtrip YUV→RGB→gamma(+cb)→YUV) ────────────────
# La branche need_rgb de _corriger_yuv est le SEUL chemin sans raccourci LUT (gamma par canal RGB,
# non-linéaire) → ~176-278 ms/trame en numpy pur (4-7× le budget). Le noyau C (cf. tools/cc_full.c,
# -O3 -mavx2 -ffp-contract=off, pthreads glibc-only) fusionne tout le pipeline en UNE passe threadée.
# Le .so pré-buildé (x86_64, seule dépendance glibc/libm, -mavx2 : toute la flotte Xeon Scalable)
# EMBARQUÉ en base64, écrit dans /tmp au démarrage (écriture ATOMIQUE), chargé par ctypes. TOUTE
# anomalie (arch, AVX2, ABI, écriture, chargement, formes) → repli numpy INCHANGÉ (bit-exact avec
# lui-même). L'arithmétique conversion/color-balance est bit-exacte vs numpy (float32, même ordre) ;
# seul le gamma pow diffère de ≤ quelques LSB (mesuré par tools/equiv_ccfull.py). Le blob est
# régénéré par tools/embed_ccfull.py après un build (tools/build_ccfull.sh) — NE PAS éditer à la main.
_CCFULL_SO_B64 = """
f0VMRgIBAQAAAAAAAAAAAAMAPgABAAAAAAAAAAAAAABAAAAAAAAAACBhAAAAAAAAAAAAAEAAOAAJAEAAGgAZAAEAAAAEAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAuAcAAAAAAAC4BwAAAAAAAAAQAAAAAAAAAQAAAAUAAAAAEAAAAAAAAAAQAAAAAAAA
ABAAAAAAAAAdNQAAAAAAAB01AAAAAAAAABAAAAAAAAABAAAABAAAAABQAAAAAAAAAFAAAAAAAAAAUAAAAAAAADQEAAAAAAAA
NAQAAAAAAAAAEAAAAAAAAAEAAAAGAAAAqF0AAAAAAACobQAAAAAAAKhtAAAAAAAAeAIAAAAAAACAAgAAAAAAAAAQAAAAAAAA
AgAAAAYAAAC4XQAAAAAAALhtAAAAAAAAuG0AAAAAAADgAQAAAAAAAOABAAAAAAAACAAAAAAAAAAEAAAABAAAADgCAAAAAAAA
OAIAAAAAAAA4AgAAAAAAACQAAAAAAAAAJAAAAAAAAAAEAAAAAAAAAFDldGQEAAAAoFAAAAAAAACgUAAAAAAAAKBQAAAAAAAA
XAAAAAAAAABcAAAAAAAAAAQAAAAAAAAAUeV0ZAYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
EAAAAAAAAABS5XRkBAAAAKhdAAAAAAAAqG0AAAAAAACobQAAAAAAAFgCAAAAAAAAWAIAAAAAAAABAAAAAAAAAAQAAAAUAAAA
AwAAAEdOVQAmLdZDNm74Z/+Ta97bzLHsdMWiuQAAAAADAAAADQAAAAEAAAAGAAAAAAACIRAyAEANAAAADwAAAAAAAABkdGiI
KavQQpgP5pxtF6lCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJAAAAASAAAAAAAAAAAAAAAAAAAAAAAAAO0AAAASAAAA
AAAAAAAAAAAAAAAAAAAAANQAAAASAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAOMAAAASAAAA
AAAAAAAAAAAAAAAAAAAAAMAAAAASAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAIEAAAASAAAA
AAAAAAAAAAAAAAAAAAAAACwAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAEYAAAAiAAAAAAAAAAAAAAAAAAAAAAAAAJ0AAAASAAAA
AAAAAAAAAAAAAAAAAAAAAK8AAAASAAAAAAAAAAAAAAAAAAAAAAAAAFUAAAASAAwAYEMAAAAAAACxAQAAAAAAAHYAAAASAAwA
oDkAAAAAAADcBAAAAAAAAMgAAAASAAwAgD4AAAAAAADcBAAAAAAAAGIAAAASAAwAkDkAAAAAAAAGAAAAAAAAAABfX2dtb25f
c3RhcnRfXwBfSVRNX2RlcmVnaXN0ZXJUTUNsb25lVGFibGUAX0lUTV9yZWdpc3RlclRNQ2xvbmVUYWJsZQBfX2N4YV9maW5h
bGl6ZQBjY19nYW1tYV9yb3cAY2NfZnVsbF9hYmlfdmVyc2lvbgBjY19mdWxsX3U4AHB0aHJlYWRfY3JlYXRlAHB0aHJlYWRf
am9pbgBzY2hlZF9nZXRhZmZpbml0eQBfX3NjaGVkX2NwdWNvdW50AHN5c2NvbmYAY2NfZnVsbF91MTYAX1pHVmROOHZ2X3Bv
d2YAX1pHVmJONHZ2X3Bvd2YAbGlibS5zby42AGxpYm12ZWMuc28uMQBsaWJjLnNvLjYAR0xJQkNfMi4yMgBHTElCQ18yLjI3
AEdMSUJDXzIuNgBHTElCQ18yLjMuNABHTElCQ18yLjIuNQBHTElCQ18yLjM0AAAAAgADAAQAAQAEAAUAAQACAAEABQAGAAcA
AQABAAEAAQABAAEA/AAAABAAAAAgAAAAgpGWBgAABAATAQAAAAAAAAEAAQDyAAAAEAAAACAAAACHkZYGAAADAB4BAAAAAAAA
AQAEAAkBAAAQAAAAAAAAABZpaQ0AAAcAKQEAABAAAAB0GWkJAAAGADMBAAAQAAAAdRppCQAABQA/AQAAEAAAALSRlgYAAAIA
SwEAAAAAAACobQAAAAAAAAgAAAAAAAAAMBEAAAAAAACwbQAAAAAAAAgAAAAAAAAA8BAAAAAAAAAYcAAAAAAAAAgAAAAAAAAA
GHAAAAAAAACYbwAAAAAAAAYAAAABAAAAAAAAAAAAAACgbwAAAAAAAAYAAAAEAAAAAAAAAAAAAACobwAAAAAAAAYAAAAGAAAA
AAAAAAAAAACwbwAAAAAAAAYAAAAHAAAAAAAAAAAAAAC4bwAAAAAAAAYAAAAIAAAAAAAAAAAAAADAbwAAAAAAAAYAAAAJAAAA
AAAAAAAAAADIbwAAAAAAAAYAAAAKAAAAAAAAAAAAAADQbwAAAAAAAAYAAAALAAAAAAAAAAAAAADYbwAAAAAAAAYAAAANAAAA
AAAAAAAAAADgbwAAAAAAAAYAAAAMAAAAAAAAAAAAAAAAcAAAAAAAAAcAAAACAAAAAAAAAAAAAAAIcAAAAAAAAAcAAAADAAAA
AAAAAAAAAAAQcAAAAAAAAAcAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
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
xfoQNsX6EbWgf/3/xfoQN0iNvdD//f/F+hG1nH/9/8X6EDX5LgAAkMV6EByCxXoQFIfFolmNyH/9/8X6ECSBxapZhcB//f/F
8ljIxdpZhbx//f/F8ljIxMFyXsjF8ljJxcJcwcXyXM/FeC/oD4ceAQAAxcJdwMV4L+nEQTBXycXCXOgPhg8BAADF0lmNoH/9
/8X6WZWcf/3/xepY0cWyWY2kf/3/xepY0cX6WY2of/3/xfpZhbh//f/F6lnTxMFqWNPFUlmdrH/9/8WCWe3FeC/qxMFyWMvF
MlmdtH/9/8UyWY2wf/3/xfpYxcTBcljLxMF6WMHF8lnLxfpZw8TBcljKxfpYxA+HzAAAAMV4L+nFul3SxepZJSAuAAAPh8gA
AADFul3JxXJZDRIuAADFeC/oD4eQAAAAxbpdwMX6WS0ALgAAxfoRBIHEwVpYwcX6ERSCxfoRDIfF+ljFxfpYxsTBelnGxXgv
6Hc9xZpdyMX6LPFBiDQESIPAAUg52A+FpP7//+kM9P//Dx+AAAAAAMX4KO/F+FfAxUJdycTBUlzp6eP+//9mkEHGBAQASIPA
AUg5ww+Fbv7//+nW8///Zg8fhAAAAAAAxdBX7cX4KMXpb////w8fAMV4L+nF2Ffkxfgo1A+GOP///8RBMFfJxXgpyek2////
xfoQHRwtAADF+hA9TC0AAEiNldB//f9Ijb3Q//3/xfoQNTotAADF+hAVNi0AAEiNjdB//v/F8FfJxfoQLSctAADF+hAlIy0A
AA8fgAAAAADF+hAEAsV6EBQHMfbFehAMAcUqWd7FelnHxSpZ1cX6WcLEQTpcw8UyWdrFMlnMxMF6XMLEQTpYw8TBelzBxTpY
w8X6WMPEQTpZxsTBelnGxMF4L8h3CsRBGl3AxMF6LPDF+C/ITIuNkH/9/0OINCl3L8WaXcBMi42If/3/xfos8EOINClIi7VI
f/3/SYPFAUgB8E05xQ+FZf///+mx8///SIu1iH/9/0LGBC4ASIu1SH/9/0mDxQFIAfBNOegPhT3////pifP//8X4d0iBxOCA
AgBbQVpBXEFdQV5BX11JjWL4w0iDhXB//f8BSIuFcH/9/0g5hQh//f9+zUyLrYB//f9MD6/o6SXu//9Ig4Vwf/3/AUiLhXB/
/f9IOYUIf/3/fqVIi7WAf/3/SYn1TA+v6EiF9g+PCPb//0iLhRh//f/pru///0SLpZh//f9FheR+DEiJhXB//f/paO///0iD
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
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIA/AAAAQwAAgEH0/ZQ/uknMPzm0yD7FIFA/hxYBQAAAf0OBlYM+
JQYBPzm0yD2HFpk+okUWP9V46T1QjRe+9P2UPpzE4D5/arw+c2iRPQAAAAAAAAAAAAAAAAAAAACBgIA7gYCAO4GAgDuBgIA7
gYCAO4GAgDuBgIA7gYCAOwAAf0MAAH9DAAB/QwAAf0MAAH9DAAB/QwAAf0MAAH9DARsDO1wAAAAKAAAAgL///3gAAADAv///
oAAAAKDA//+4AAAAENT//wABAACA1P//PAEAAIDo//+EAQAA8Oj//8ABAAAA6f//1AEAAODt//+MAgAAwPL//0gDAAAAAAAA
FAAAAAAAAAABelIAAXgQARsMBwiQAQAAJAAAABwAAAAAv///QAAAAAAOEEYOGEoPC3cIgAA/GjsqMyQiAAAAABQAAABEAAAA
GL///wgAAAAAAAAAAAAAAEQAAABcAAAA4L///2QTAAAARQwKAEwQBgJ2AEoPA3ZYBhAPAnZ4EA4CdnAQDQJ2aBAMAnZgSBAD
AnZQA6ISCgwKAE0MBwhBCzgAAACkAAAACNP//2QAAAAARA4QXg4YRg4gQw4oQw4wQw44Qw5AQw5IQw5QQg5YTA5gQw5oQg5w
Vg4IAEQAAADgAAAAPNP///ITAAAARQwKAEwQBgJ2AEoPA3ZYBhAPAnZ4EA4CdnAQDQJ2aBAMAnZgSBADAnZQAw0TCgwKAE0M
BwhBCzgAAAAoAQAA9Ob//2QAAAAARA4QXg4YRg4gQw4oQw4wQw44Qw5AQw5IQw5QQg5YTA5gQw5oQg5wVg4IABAAAABkAQAA
KOf//wYAAAAAAAAAtAAAAHgBAAAk5///3AQAAABCDhCPAkYOGI4DQg4gjQRCDiiMBUEOMIYGQQ44gwdHDqAPA4QCDqgPRQ6w
D0cOuA9HDsAPRw7ID0cO0A9HDtgPRw7gD0gO6A9HDvAPRw74D0gOgBB/DqAPAmgKDjhBDjBBDihCDiBCDhhCDhBCDghJCwKc
DqgPRA6wD0cOuA9HDsAPRw7ID0cO0A9HDtgPRw7gD0gO6A9HDvAPRw74D0gOgBBxDqAPALgAAAAwAgAATOv//9wEAAAAQg4Q
jwJGDhiOA0IOII0EQg4ojAVBDjCGBkEOOIMHRw6gDwOEAg6oD0UOsA9HDrgPRw7AD0cOyA9HDtAPRw7YD0cO4A9IDugPRw7w
D0cO+A9IDoAQfw6gDwJoCg44QQ4wQQ4oQg4gQg4YQg4QQg4ISQsCnA6oD0QOsA9HDrgPRw7AD0cOyA9HDtAPRw7YD0cO4A9I
DugPRw7wD0cO+A9IDoAQcQ6gDwAAAAAARAAAAOwCAABw7///sQEAAABFDAoATBAGAnYASg8DdlgGEA8CdngQDgJ2cBANAnZo
EAwCdmBFEAMCdlADbwEKDAoATQwHCEQLAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
MBEAAAAAAADwEAAAAAAAAAEAAAAAAAAA8gAAAAAAAAABAAAAAAAAAPwAAAAAAAAAAQAAAAAAAAAJAQAAAAAAAAwAAAAAAAAA
ABAAAAAAAAANAAAAAAAAABRFAAAAAAAAGQAAAAAAAACobQAAAAAAABsAAAAAAAAACAAAAAAAAAAaAAAAAAAAALBtAAAAAAAA
HAAAAAAAAAAIAAAAAAAAAPX+/28AAAAAYAIAAAAAAAAFAAAAAAAAADAEAAAAAAAABgAAAAAAAACYAgAAAAAAAAoAAAAAAAAA
VgEAAAAAAAALAAAAAAAAABgAAAAAAAAAAwAAAAAAAADobwAAAAAAAAIAAAAAAAAASAAAAAAAAAAUAAAAAAAAAAcAAAAAAAAA
FwAAAAAAAABwBwAAAAAAAAcAAAAAAAAAOAYAAAAAAAAIAAAAAAAAADgBAAAAAAAACQAAAAAAAAAYAAAAAAAAAP7//28AAAAA
qAUAAAAAAAD///9vAAAAAAMAAAAAAAAA8P//bwAAAACGBQAAAAAAAPn//28AAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
uG0AAAAAAAAAAAAAAAAAAAAAAAAAAAAANhAAAAAAAABGEAAAAAAAAFYQAAAAAAAAGHAAAAAAAABHQ0M6IChEZWJpYW4gMTQu
Mi4wLTE5KSAxNC4yLjAAAC5zaHN0cnRhYgAubm90ZS5nbnUuYnVpbGQtaWQALmdudS5oYXNoAC5keW5zeW0ALmR5bnN0cgAu
Z251LnZlcnNpb24ALmdudS52ZXJzaW9uX3IALnJlbGEuZHluAC5yZWxhLnBsdAAuaW5pdAAucGx0LmdvdAAudGV4dAAuZmlu
aQAucm9kYXRhAC5laF9mcmFtZV9oZHIALmVoX2ZyYW1lAC5pbml0X2FycmF5AC5maW5pX2FycmF5AC5keW5hbWljAC5nb3Qu
cGx0AC5kYXRhAC5ic3MALmNvbW1lbnQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAsAAAAHAAAAAgAAAAAAAAA4AgAAAAAAADgCAAAAAAAAJAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAA
AAAAAAAAAAAeAAAA9v//bwIAAAAAAAAAYAIAAAAAAABgAgAAAAAAADQAAAAAAAAAAwAAAAAAAAAIAAAAAAAAAAAAAAAAAAAA
KAAAAAsAAAACAAAAAAAAAJgCAAAAAAAAmAIAAAAAAACYAQAAAAAAAAQAAAABAAAACAAAAAAAAAAYAAAAAAAAADAAAAADAAAA
AgAAAAAAAAAwBAAAAAAAADAEAAAAAAAAVgEAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAA4AAAA////bwIAAAAAAAAA
hgUAAAAAAACGBQAAAAAAACIAAAAAAAAAAwAAAAAAAAACAAAAAAAAAAIAAAAAAAAARQAAAP7//28CAAAAAAAAAKgFAAAAAAAA
qAUAAAAAAACQAAAAAAAAAAQAAAADAAAACAAAAAAAAAAAAAAAAAAAAFQAAAAEAAAAAgAAAAAAAAA4BgAAAAAAADgGAAAAAAAA
OAEAAAAAAAADAAAAAAAAAAgAAAAAAAAAGAAAAAAAAABeAAAABAAAAEIAAAAAAAAAcAcAAAAAAABwBwAAAAAAAEgAAAAAAAAA
AwAAABUAAAAIAAAAAAAAABgAAAAAAAAAaAAAAAEAAAAGAAAAAAAAAAAQAAAAAAAAABAAAAAAAAAXAAAAAAAAAAAAAAAAAAAA
BAAAAAAAAAAAAAAAAAAAAGMAAAABAAAABgAAAAAAAAAgEAAAAAAAACAQAAAAAAAAQAAAAAAAAAAAAAAAAAAAABAAAAAAAAAA
EAAAAAAAAABuAAAAAQAAAAYAAAAAAAAAYBAAAAAAAABgEAAAAAAAAAgAAAAAAAAAAAAAAAAAAAAIAAAAAAAAAAgAAAAAAAAA
dwAAAAEAAAAGAAAAAAAAAIAQAAAAAAAAgBAAAAAAAACRNAAAAAAAAAAAAAAAAAAAIAAAAAAAAAAAAAAAAAAAAH0AAAABAAAA
BgAAAAAAAAAURQAAAAAAABRFAAAAAAAACQAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAACDAAAAAQAAAAIAAAAAAAAA
AFAAAAAAAAAAUAAAAAAAAKAAAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAAAiwAAAAEAAAACAAAAAAAAAKBQAAAAAAAA
oFAAAAAAAABcAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAJkAAAABAAAAAgAAAAAAAAAAUQAAAAAAAABRAAAAAAAA
NAMAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAACjAAAADgAAAAMAAAAAAAAAqG0AAAAAAACoXQAAAAAAAAgAAAAAAAAA
AAAAAAAAAAAIAAAAAAAAAAgAAAAAAAAArwAAAA8AAAADAAAAAAAAALBtAAAAAAAAsF0AAAAAAAAIAAAAAAAAAAAAAAAAAAAA
CAAAAAAAAAAIAAAAAAAAALsAAAAGAAAAAwAAAAAAAAC4bQAAAAAAALhdAAAAAAAA4AEAAAAAAAAEAAAAAAAAAAgAAAAAAAAA
EAAAAAAAAAByAAAAAQAAAAMAAAAAAAAAmG8AAAAAAACYXwAAAAAAAFAAAAAAAAAAAAAAAAAAAAAIAAAAAAAAAAgAAAAAAAAA
xAAAAAEAAAADAAAAAAAAAOhvAAAAAAAA6F8AAAAAAAAwAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAIAAAAAAAAAM0AAAABAAAA
AwAAAAAAAAAYcAAAAAAAABhgAAAAAAAACAAAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAADTAAAACAAAAAMAAAAAAAAA
IHAAAAAAAAAgYAAAAAAAAAgAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAA2AAAAAEAAAAwAAAAAAAAAAAAAAAAAAAA
IGAAAAAAAAAfAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAEAAAADAAAAAAAAAAAAAAAAAAAAAAAAAD9gAAAAAAAA
4QAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAA=
"""  # fin _CCFULL_SO_B64 (généré par tools/embed_ccfull.py — ne pas éditer à la main)


def _load_ccfull():
    """Charge le noyau cc_full (u8 + u16) ou None (→ repli numpy). Chargé inconditionnellement au
    démarrage : le gamma R/G/B est réglable À CHAUD (POST /params), le noyau doit être prêt même si
    la correction initiale ne l'active pas. Renvoie {{"u8": fn, "u16": fn}} ou None."""
    try:
        import base64, ctypes, platform
        if platform.machine() != "x86_64":
            raise RuntimeError("arch " + platform.machine())
        with open("/proc/cpuinfo") as f:
            if " avx2 " not in f.read().replace("\n", " "):
                raise RuntimeError("cpu sans AVX2")
        if "PLACEHOLDER_CCFULL_SO" in _CCFULL_SO_B64:
            raise RuntimeError(".so non embarqué (embed_ccfull.py non exécuté)")
        path = "/tmp/cc_full_" + PLUGIN_VERSION.replace("/", "_") + ".so"
        # Écriture ATOMIQUE (temp + os.replace), JAMAIS open("wb") direct : tronquer un .so encore
        # mmappé par un ancien process (restart agent, double exec) SEGFAULTE ce process ; le
        # replace laisse l'ancien inode vivant pour les mappings existants.
        import tempfile
        fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".so")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(_CCFULL_SO_B64))
            os.replace(tmp, path)
        except BaseException:
            try: os.unlink(tmp)
            except OSError: pass
            raise
        lib = ctypes.CDLL(path)
        if lib.cc_full_abi_version() != 1:
            raise RuntimeError("ABI noyau inattendue")
        # y,u,v (ptr) ; h,w,uv_h,uv_w (int64) ; ch,cw (int32) ; sc,maxf (float) ; ig[3] (ptr) ;
        # gon[3] (ptr int32) ; cb_on (int32) ; cbs,cbm,cbh (ptr) ; yo,uo,vo (ptr) ;
        # nthreads (int32, 0 = auto = cœurs de l'affinité — le cpuset du conteneur compute borne).
        argt = ([ctypes.c_void_p] * 3 + [ctypes.c_int64] * 4 + [ctypes.c_int32] * 2
                + [ctypes.c_float] * 2 + [ctypes.c_void_p] * 2 + [ctypes.c_int32]
                + [ctypes.c_void_p] * 6 + [ctypes.c_int32])
        out = {{}}
        for depth_key, sym in (("u8", "cc_full_u8"), ("u16", "cc_full_u16")):
            fn = getattr(lib, sym)
            fn.restype = ctypes.c_int
            fn.argtypes = argt
            out[depth_key] = fn
        return out
    except Exception as e:
        log(f"noyau cc_full indisponible ({{e}}) → repli numpy (roundtrip RGB ~176-278 ms/trame)",
            "warning")
        return None


_CCFULL_LIB = _load_ccfull()


def _corriger_full_c(y, u, v, p, lyt):
    """Branche « full » (roundtrip RGB : gamma R/G/B + color balance) via le noyau C fusionné.
    Renvoie (y, u, v) à la profondeur cible, ou None si le noyau est indisponible / formes
    inattendues → repli numpy. Opère sur la trame ENTIÈRE ou une BANDE (les dims sont LUES des
    tableaux, pas de lyt) : y (h×w), u/v (uv_h×uv_w) avec uv_h·ch == h (bandes alignées chroma)."""
    lib = _CCFULL_LIB
    if lib is None:
        return None
    np_dt = lyt["np_dt"]
    if y.dtype != np_dt or u.dtype != np_dt or v.dtype != np_dt:
        return None
    if y.ndim != 2 or u.ndim != 2 or v.ndim != 2:
        return None
    h, w = y.shape
    uv_h, uv_w = u.shape
    ch = lyt["ch"]; cw = lyt["cw"]
    if (u.shape != v.shape or w != lyt["width"] or uv_w != lyt["uv_w"]
            or uv_h * ch != h):
        return None
    yc = np.ascontiguousarray(y); uc = np.ascontiguousarray(u); vc = np.ascontiguousarray(v)
    yo = np.empty((h, w), np_dt); uo = np.empty((uv_h, uv_w), np_dt); vo = np.empty((uv_h, uv_w), np_dt)
    gr = p["gamma_r"]; gg = p["gamma_g"]; gb = p["gamma_b"]
    ig = np.array([1.0 / gr if gr > 0 else 1.0,
                   1.0 / gg if gg > 0 else 1.0,
                   1.0 / gb if gb > 0 else 1.0], dtype=np.float32)
    gon = np.array([1 if (gr != 1.0 and gr > 0) else 0,
                    1 if (gg != 1.0 and gg > 0) else 0,
                    1 if (gb != 1.0 and gb > 0) else 0], dtype=np.int32)
    cb_on = 1 if any(p[k] != 0.0 for k in _CB_KEYS) else 0
    cbs = np.array([p["cb_rs"], p["cb_gs"], p["cb_bs"]], dtype=np.float32)
    cbm = np.array([p["cb_rm"], p["cb_gm"], p["cb_bm"]], dtype=np.float32)
    cbh = np.array([p["cb_rh"], p["cb_gh"], p["cb_bh"]], dtype=np.float32)
    fn = lib["u16"] if lyt["deep"] else lib["u8"]
    rc = fn(yc.ctypes.data, uc.ctypes.data, vc.ctypes.data,
            int(h), int(w), int(uv_h), int(uv_w), int(ch), int(cw),
            float(lyt["scale"]), float(lyt["maxf"]),
            ig.ctypes.data, gon.ctypes.data, int(cb_on),
            cbs.ctypes.data, cbm.ctypes.data, cbh.ctypes.data,
            yo.ctypes.data, uo.ctypes.data, vo.ctypes.data, 0)
    if rc != 0:
        return None
    return yo, uo, vo


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
        # Chemin RAPIDE : noyau C fusionné (une passe threadée). Repli numpy si indispo/formes.
        res_c = _corriger_full_c(y, u, v, p, lyt)
        if res_c is not None:
            return res_c
        # ── Repli numpy (INCHANGÉ, bit-exact avec lui-même) ──
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
