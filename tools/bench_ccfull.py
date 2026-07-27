"""Perf de la branche « full » du correcteur : noyau C fusionné vs repli numpy, sur le SCRIPT
RENDU du plugin. Cible : SOUS le budget 40 ms (25p), idéalement < 20 ms (50p). Numpy pur mesuré
~176 ms (gamma RGB seul) à ~265 ms (pire cas). 1080p 8b 422 par défaut.

Mesure appliquer_correction() (chemin réel de la boucle : split → _corriger_yuv → join) sur les
cas full, C actif vs C neutralisé (_CCFULL_LIB=None), + scaling threads du seul cœur C.

    docker run --rm --cpuset-cpus=22-23,46-47 -v /tmp/cc.py:/tmp/cc.py \
        -v .../bench_ccfull.py:/tmp/bench.py --entrypoint python3 bobi-compute:0.17 \
        /tmp/bench.py /tmp/cc.py
"""
import ast, sys, time
import numpy as np

BANNED = ("HTTPServer", "serve_forever", ".start()", "Instance(", "Writer(", "Reader(",
          "socket.", "while True", "signal.signal(", "threading.Thread")
NEEDED = {"_make_layout", "DEFAULT_PARAMS", "_norm_params", "appliquer_correction",
          "_corriger_full_c", "_CCFULL_LIB", "split_yuv", "_corriger_yuv"}


def load_ns(src):
    import bobimxl  # noqa: F401
    ns = {"__name__": "cc_bench"}
    for node in ast.parse(src).body:
        if any(b in ast.unparse(node) for b in BANNED):
            continue
        try:
            exec(compile(ast.Module([node], []), "<cc>", "exec"), ns)
        except Exception:
            pass
    missing = NEEDED - set(ns)
    if missing:
        raise SystemExit("fonctions manquantes : %s" % sorted(missing))
    return ns


def bench(fn, *args, n=30):
    for _ in range(3):
        fn(*args)
    t0 = time.perf_counter()
    for _ in range(n):
        fn(*args)
    return (time.perf_counter() - t0) / n * 1000.0


def main(path):
    ns = load_ns(open(path).read())
    native = ns["_CCFULL_LIB"]
    print("noyau C chargé :", native is not None)
    W, H = 1920, 1080
    lyt = ns["_make_layout"](W, H, "422", 8)
    dt = lyt["np_dt"]
    rng = np.random.default_rng(42)
    y = rng.integers(16, 235, (H, W), dtype=np.int64).astype(dt)
    u = rng.integers(16, 240, (H, lyt["uv_w"]), dtype=np.int64).astype(dt)
    v = rng.integers(16, 240, (H, lyt["uv_w"]), dtype=np.int64).astype(dt)
    yuv = y.tobytes() + u.tobytes() + v.tobytes()

    def p(**kw):
        d = dict(ns["DEFAULT_PARAMS"]); d.update(kw); return ns["_norm_params"](d)

    cases = {
        "gamma RGB seul":            p(gamma_r=1.3, gamma_g=0.8, gamma_b=1.1),
        "gamma RGB + cb + hue + sat (pire cas)": p(
            gamma_r=1.3, gamma_g=0.8, gamma_b=1.1, hue=20.0, saturation=1.3,
            cb_rm=0.2, cb_gs=-0.1, cb_bh=0.15),
    }

    print("=== appliquer_correction() 1080p 8b 422, ms/trame (moy 30 it.) ===")
    for name, params in cases.items():
        ns["_CCFULL_LIB"] = native
        ms_c = bench(ns["appliquer_correction"], yuv, params, lyt, n=30)
        ns["_CCFULL_LIB"] = None
        ms_np = bench(ns["appliquer_correction"], yuv, params, lyt, n=20)
        ns["_CCFULL_LIB"] = native
        spd = ms_np / ms_c if ms_c else 0
        print("  %-40s  C %7.2f ms | numpy %7.2f ms | ×%.1f" % (name, ms_c, ms_np, spd))

    if native is not None:
        print("=== scaling threads (cœur C seul, gamma RGB seul, ms/trame) ===")
        params = cases["gamma RGB seul"]
        y2, u2, v2 = ns["split_yuv"](yuv, lyt)

        def run_c(nt):
            lib = ns["_CCFULL_LIB"]
            fn = lib["u8"]
            yo = np.empty_like(y2); uo = np.empty_like(u2); vo = np.empty_like(v2)
            ig = np.array([1/1.3, 1/0.8, 1/1.1], np.float32)
            gon = np.array([1, 1, 1], np.int32)
            cbs = np.zeros(3, np.float32); cbm = np.zeros(3, np.float32); cbh = np.zeros(3, np.float32)
            fn(y2.ctypes.data, u2.ctypes.data, v2.ctypes.data, H, W, lyt["uv_h"], lyt["uv_w"],
               int(lyt["ch"]), int(lyt["cw"]), float(lyt["scale"]), float(lyt["maxf"]),
               ig.ctypes.data, gon.ctypes.data, 0, cbs.ctypes.data, cbm.ctypes.data,
               cbh.ctypes.data, yo.ctypes.data, uo.ctypes.data, vo.ctypes.data, nt)
        for nt in (1, 2, 4, 0):
            ms = bench(run_c, nt, n=50)
            print("  C x%-4s %7.2f ms" % ("auto" if nt == 0 else nt, ms))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cc.py")
