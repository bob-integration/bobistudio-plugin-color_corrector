"""Équivalence de la branche « full » du correcteur : noyau C fusionné (_corriger_full_c via
_CCFULL_LIB) ≈ repli numpy (_corriger_yuv avec _CCFULL_LIB neutralisé), sur le SCRIPT RENDU du
plugin (str.format), en 8/10/12 bits × 420/422/444.

DEUX propriétés :
  (1) C ≈ numpy : la conversion YUV↔RGB et le color-balance sont BIT-EXACTS (float32, même ordre) ;
      seul le gamma pow diffère de ≤ quelques LSB flottant → après quantification, écart entier
      borné. On MESURE max|Δ| (Y, U, V) et le % de pixels différents. Plafond dur : max|Δ| ≤ 2 LSB
      en 8 bits (proportionnel : 8 en 10 bits, 32 en 12 bits).
  (2) BANDE ≡ PLEIN CADRE : _corriger_bande (plan "full") sur des bandes disjointes produit
      EXACTEMENT les octets du plein cadre — pour le chemin C ET le chemin numpy (array_equal).

Méthode (modèle tools/equiv_bwdif.py / equiv_mixer.py) : rendre le script, l'exec nœud par nœud
dans un namespace neuf (garde-fous serveurs/boucles), vérifier que le noyau C est CHARGÉ (sinon
l'équivalence ne prouve rien), puis comparer. Le toggle numpy = mettre ns["_CCFULL_LIB"]=None.

⚠ À exécuter DANS l'image runtime (bobi-compute, x86_64+AVX2), le script rendu étant fourni par
l'orchestrateur :
    ./venv/bin/python -c "from app import plugins; \
        open('/tmp/cc.py','w').write(plugins.render_script('color_corrector', \
        dict(plugins.get('color_corrector')['deploy_defaults']), 'equiv'))"
    docker run --rm -v /tmp/cc.py:/tmp/cc.py -v .../equiv_ccfull.py:/tmp/eq.py \
        --entrypoint python3 bobi-compute:0.17 /tmp/eq.py /tmp/cc.py
Sans argument, le script se rend lui-même via app.plugins (contrôleur) pour les profils ci-dessous.
"""
import ast, sys
import numpy as np

NEEDED = {"_make_layout", "DEFAULT_PARAMS", "_norm_params", "_corriger_yuv", "_corriger_full_c",
          "_corriger_bande", "_plan_bande", "_CCFULL_LIB", "_CB_KEYS", "split_yuv"}
BANNED = ("HTTPServer", "serve_forever", ".start()", "Instance(", "Writer(", "Reader(",
          "socket.", "while True", "signal.signal", "threading.Thread")


def load_ns(src):
    import bobimxl  # noqa: F401  (le script rendu l'importe ; présent dans bobi-compute)
    ns = {"__name__": "cc_equiv"}
    for node in ast.parse(src).body:
        if any(b in ast.unparse(node) for b in BANNED):
            continue
        try:
            exec(compile(ast.Module([node], []), "<cc>", "exec"), ns)
        except Exception:
            pass
    missing = NEEDED - set(ns)
    if missing:
        raise SystemExit("fonctions manquantes dans le rendu : %s" % sorted(missing))
    return ns


PROFILS = ((8, "420"), (8, "422"), (8, "444"),
           (10, "420"), (10, "422"), (10, "444"),
           (12, "420"), (12, "422"), (12, "444"))


def rand_planes(lyt, rng):
    maxv = int(lyt["maxf"])
    dt = lyt["np_dt"]
    y = rng.integers(0, maxv + 1, (lyt["height"], lyt["width"]), dtype=np.uint32).astype(dt)
    u = rng.integers(0, maxv + 1, (lyt["uv_h"], lyt["uv_w"]), dtype=np.uint32).astype(dt)
    v = rng.integers(0, maxv + 1, (lyt["uv_h"], lyt["uv_w"]), dtype=np.uint32).astype(dt)
    return y, u, v


def check(ns, bit_depth, chroma):
    W, H = 160, 64          # divisible par cw (2) et ch (2 en 420) — plein cadre
    lyt = ns["_make_layout"](W, H, chroma, bit_depth)
    dt = lyt["np_dt"]
    maxv = int(lyt["maxf"])
    lsb_ceiling = 2 * lyt["scale"]      # 2 LSB 8 bits → proportionnel en 10/12 bits
    rng = np.random.default_rng(20260727)

    def p(**kw):
        d = dict(ns["DEFAULT_PARAMS"]); d.update(kw); return ns["_norm_params"](d)

    cases = {
        "gamma RGB seul":            p(gamma_r=1.3, gamma_g=0.8, gamma_b=1.1),
        "gamma RGB + cb":            p(gamma_r=1.3, gamma_g=0.8, gamma_b=1.1,
                                       cb_rm=0.2, cb_gs=-0.1, cb_bh=0.15,
                                       cb_rs=0.05, cb_gh=-0.2),
        "gamma RGB + cb + sat/hue":  p(gamma_r=1.4, gamma_g=0.7, gamma_b=1.2, saturation=1.3,
                                       hue=20.0, cb_rm=0.2, cb_gs=-0.1, cb_bh=0.15),
        "gamma R seul (G,B=1)":      p(gamma_r=0.5),
        "gamma extrêmes":            p(gamma_r=10.0, gamma_g=0.1, gamma_b=3.3),
    }

    native = ns["_CCFULL_LIB"] is not None
    worst = 0.0
    fails = 0
    band_fail = 0

    for name, params in cases.items():
        y0, u0, v0 = rand_planes(lyt, rng)

        # (1) PLEIN CADRE : C vs numpy. Preuve que le C a bien tourné : _corriger_full_c non-None.
        ns["_CCFULL_LIB"] = native_lib               # C actif
        if native and ns["_corriger_full_c"](y0.copy(), u0.copy(), v0.copy(), params, lyt) is None:
            print("  !! _corriger_full_c a renvoyé None (repli) sur '%s' → C NON exercé" % name)
            fails += 1
        yc, uc, vc = ns["_corriger_yuv"](y0.copy(), u0.copy(), v0.copy(), params, lyt)
        ns["_CCFULL_LIB"] = None                     # neutralise le C → numpy pur
        yn, un, vn = ns["_corriger_yuv"](y0.copy(), u0.copy(), v0.copy(), params, lyt)
        ns["_CCFULL_LIB"] = native_lib

        dmax = 0; ndiff = 0; ntot = 0
        for a, b in ((yc, yn), (uc, un), (vc, vn)):
            d = np.abs(a.astype(np.int64) - b.astype(np.int64))
            dmax = max(dmax, int(d.max()) if d.size else 0)
            ndiff += int((d != 0).sum()); ntot += d.size
        worst = max(worst, dmax)
        ok = dmax <= lsb_ceiling
        if not ok:
            fails += 1
        print("    %-28s max|Δ|=%3d LSB (plafond %d)  pix≠ %.3f%%  %s"
              % (name, dmax, lsb_ceiling, 100.0 * ndiff / max(1, ntot),
                 "OK" if ok else "DÉPASSE"))

        # (2) BANDE ≡ PLEIN CADRE (C actif), pour le chemin "full" — bandes multiples de ch.
        band = max(lyt["ch"], (H // 8) - ((H // 8) % lyt["ch"]))
        for lib_lbl, lib_val in (("C", native_lib), ("numpy", None)):
            ns["_CCFULL_LIB"] = lib_val
            yf, uf, vf = ns["_corriger_yuv"](y0.copy(), u0.copy(), v0.copy(), params, lyt)
            plan = ns["_plan_bande"](params, lyt)
            g_y = y0.copy(); g_u = u0.copy(); g_v = v0.copy()
            ys, us, vs = y0.copy(), u0.copy(), v0.copy()
            for a in range(0, H, band):
                bb = min(H, a + band)
                ns["_corriger_bande"](plan, params, lyt, ys, us, vs, g_y, g_u, g_v, a, bb)
            eq = (np.array_equal(g_y, yf) and np.array_equal(g_u, uf)
                  and np.array_equal(g_v, vf))
            if not eq:
                band_fail += 1
                print("    BANDE≠PLEIN (%s) sur '%s' [plan=%s]" % (lib_lbl, name, plan[0]))
            ns["_CCFULL_LIB"] = native_lib

    return native, worst, fails, band_fail


srcs = ([("script fourni", open(sys.argv[1]).read())] if len(sys.argv) > 1 else None)
if srcs is None:
    sys.path.insert(0, "/opt/bobistudio")
    from app import plugins
    base = dict(plugins.get("color_corrector")["deploy_defaults"])
    srcs = [("app.plugins", plugins.render_script("color_corrector", base, "equiv"))]

total_fail = 0
for label, src in srcs:
    print("== source", label)
    ns = load_ns(src)
    native_lib = ns["_CCFULL_LIB"]
    print("   noyau C chargé :", native_lib is not None,
          "" if native_lib is not None else "→ ÉQUIVALENCE NON PROBANTE (repli numpy)")
    if native_lib is None:
        total_fail += 1
    for bd, ch in PROFILS:
        print("  -- %d bits %s" % (bd, ch))
        native, worst, fails, band_fail = check(ns, bd, ch)
        total_fail += fails + band_fail
        print("     → max|Δ| profil = %d LSB ; %d dépassement(s), %d échec(s) bande≡plein"
              % (worst, fails, band_fail))

print("Total échecs :", total_fail)
sys.exit(1 if total_fail else 0)
