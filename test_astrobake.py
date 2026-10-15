#!/usr/bin/env python3
"""
Regression tests for astrobake.  Run:  python3 test_astrobake.py [name ...]

The stacking tests use the local test frames (main/, dark/, flat/, bias/ next to this file, not in git)
and are skipped without them. They stack a small subset (20-40 seconds each) and check the result
against numbers measured on known-good output, so a change that makes stacks blurrier, noisier or
loses stars fails. The synthetic tests need no data.
"""
import os, sys, math, glob, shutil, tempfile, time
import numpy as np, cv2
import astrobake as ab

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = {k: os.path.join(HERE, k) for k in ("main", "dark", "flat", "bias")}
HAVE_DATA = all(ab.raw_files(d) for d in DATA.values())
SUBSET = {"main": 12, "dark": 6, "flat": 10, "bias": 10}
CROP = (2048, 3072)          # stack only the middle of the sensor: a quarter of the memory and time


def cropped(read_raw):
    """read_raw that keeps the middle CROP of the sensor (with even offsets, so the Bayer pattern holds)."""
    def read(path):
        a, *rest = read_raw(path)
        h, w = a.shape; ch, cw = min(CROP[0], h), min(CROP[1], w)
        y0, x0 = (h - ch) // 2 & ~1, (w - cw) // 2 & ~1
        return (np.ascontiguousarray(a[y0:y0 + ch, x0:x0 + cw]), *rest)
    return read


ab.read_raw = cropped(ab.read_raw)


def subset_dirs(tmp, convert=None):
    """Symlinked (or, with `convert`, converted) subsets of the test frames, in folders stack() finds."""
    dirs = {}
    for k, n in SUBSET.items():
        d = os.path.join(tmp, k); os.makedirs(d)
        files = ab.raw_files(DATA[k])
        for p in [files[i] for i in np.linspace(0, len(files) - 1, n).round().astype(int)]:
            if convert: convert(p, os.path.join(d, os.path.splitext(os.path.basename(p))[0] + ".fits"))
            else: os.symlink(p, os.path.join(d, os.path.basename(p)))
        dirs[k] = d
    return dirs


def reference_find_stars(L, n=150):
    """The original astrobake.find_stars, frozen so the tests' measurements stay comparable with BASELINE
    when the stacking code's own star finder changes."""
    h, w = L.shape
    bg = cv2.resize(cv2.medianBlur(cv2.resize(L, (w // 16, h // 16), interpolation=cv2.INTER_AREA), 5),
                    (w, h), interpolation=cv2.INTER_LINEAR)
    d = L - bg; del bg
    sm = cv2.GaussianBlur(d, (0, 0), 1.2)
    s = sm[::3, ::3]; noise = 1.4826 * float(np.median(np.abs(s - np.median(s))))
    peak = (sm == cv2.dilate(sm, np.ones((7, 7), np.uint8))) & (sm > 6 * noise)
    peak[:8] = peak[-8:] = False; peak[:, :8] = peak[:, -8:] = False
    ys, xs = np.nonzero(peak)
    if len(xs) == 0: return np.zeros((0, 2)), 0.0
    o = np.argsort(-sm[ys, xs])[:2000]; ys, xs = ys[o], xs[o]
    r = np.arange(-5, 6)
    win = np.clip(d[ys[:, None, None] + r[None, :, None], xs[:, None, None] + r[None, None, :]], 0, None)
    flux = win.sum((1, 2)) + 1e-12
    cx = (win * r[None, None, :]).sum((1, 2)) / flux; cy = (win * r[None, :, None]).sum((1, 2)) / flux
    fwhm = 2 * np.sqrt((win >= 0.5 * win.max((1, 2), keepdims=True)).sum((1, 2)) / np.pi)   # from the half-max area
    top = L[ys, xs] < 0.95 * float(L.max())                 # skip saturated cores, they centroid poorly
    ok = (fwhm > 1.5) & (fwhm < 9) & top & (np.hypot(cx, cy) < 2)   # rejects hot pixels and smudges
    o = np.argsort(-flux[ok])[:n]
    pts = np.stack([xs[ok][o] + cx[ok][o], ys[ok][o] + cy[ok][o]], 1)
    return pts, float(np.median(fwhm[ok][o])) if ok.any() else 0.0


def measure(path):
    """Stars found, their median FWHM and the background noise of a stacked TIFF (on luminance)."""
    a = ab.load(path)
    L = np.ascontiguousarray(a.mean(2))
    pts, fwhm = reference_find_stars(L, n=5000)
    return {"stars": len(pts), "fwhm": round(fwhm, 3), "noise": measure_noise(L), "shape": a.shape}


def run_stack(dirs, **kw):
    t = time.monotonic()
    r = ab.stack(dirs["main"], dirs.get("dark"), dirs.get("flat"), dirs.get("bias"), **kw)
    m = measure(r["file"])
    print(f"    {r['used']}/{r['total']} frames, {r['rejected']}% rejected, {m['stars']} stars, "
          f"FWHM {m['fwhm']}, noise {m['noise']:.3e}, {time.monotonic() - t:.0f} s")
    return r, m


# the 12-frame subset (cropped) stacked by the original kappa-sigma code, unweighted; noise is in units of a
# saturated pixel. Changes may improve on these, not fall short of them.
BASELINE = {"stars": 1955, "fwhm": 4.918, "noise": 1.0165e-04}


def check_against_baseline(m):
    assert m["stars"] >= 0.95 * BASELINE["stars"], f"fewer stars: {m['stars']} vs {BASELINE['stars']}"
    assert m["fwhm"] <= 1.03 * BASELINE["fwhm"], f"blurrier: FWHM {m['fwhm']} vs {BASELINE['fwhm']}"
    assert m["noise"] <= 1.03 * BASELINE["noise"], f"noisier: {m['noise']:.3e} vs {BASELINE['noise']:.3e}"


# ---------------------------------------------------------------- FITS files for the tests
def write_fits(path, a, **keys):
    """Minimal FITS writer: uint16 (as BZERO=32768), int16 or float32; 2D, or (3, H, W) for colour.
    Rows are stored as given; pass ROWORDER="TOP-DOWN" to mark that, or flip a first for the FITS default."""
    bitpix = {np.dtype(np.uint16): 16, np.dtype(np.int16): 16, np.dtype(np.float32): -32}[a.dtype]
    cards = [("SIMPLE", True), ("BITPIX", bitpix), ("NAXIS", a.ndim)]
    cards += [(f"NAXIS{i + 1}", n) for i, n in enumerate(a.shape[::-1])]
    if a.dtype == np.uint16: cards += [("BZERO", 32768), ("BSCALE", 1)]
    cards += list(keys.items())
    def fmt(v):
        if isinstance(v, bool): return f"{'T' if v else 'F':>20}"
        if isinstance(v, str): return f"'{v.replace(chr(39), chr(39) * 2):<8}'"
        return f"{v:>20}"
    hdr = "".join(f"{k:<8}= {fmt(v)}".ljust(80) for k, v in cards) + "END".ljust(80)
    hdr += " " * (-len(hdr) % 2880)
    data = (a ^ 0x8000).astype(">u2") if a.dtype == np.uint16 else a.astype(a.dtype.newbyteorder(">"))
    data = data.tobytes(); data += b"\0" * (-len(data) % 2880)
    with open(path, "wb") as f: f.write(hdr.encode("ascii") + data)


def raw_to_fits(bottom_up=False, mono=False, trail=None):
    """A convert function for subset_dirs: the camera raw's sensor data as a FITS frame. Colour frames keep
    the Bayer mosaic (BAYERPAT); mono frames bin each 2x2 cell into one pixel, like a mono sensor would see.
    `trail` = (path, value) draws a satellite trail of that many ADU across that light."""
    def convert(src, dst):
        a, bayer, black, white, wb = ab.read_raw(src)
        if mono:
            h, w = a.shape
            a = (a.reshape(h // 2, 2, w // 2, 2).astype(np.uint32).sum((1, 3)) // 4).astype(np.uint16)
        if trail and os.path.basename(src) == trail[0]:
            h, w = a.shape
            t = np.zeros((h, w), np.uint8); cv2.line(t, (0, h // 5), (w - 1, h // 2), 1, 5)
            a = np.minimum(a.astype(np.int32) + t.astype(np.int32) * trail[1], 65535).astype(np.uint16)
        keys = {}
        if bottom_up:
            a = a[::-1]
            if not mono: keys["BAYERPAT"] = bayer[2:] + bayer[:2]     # even height: the rows swap
        else:
            keys["ROWORDER"] = "TOP-DOWN"
            if not mono: keys["BAYERPAT"] = bayer
        write_fits(dst, np.ascontiguousarray(a), **keys)
    return convert


def test_fits_reader():
    rng = np.random.default_rng(1)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "x.fits")
        u = rng.integers(0, 65536, (5, 7), dtype=np.uint16)
        write_fits(p, u, ROWORDER="TOP-DOWN", OBJECT="it's M31")
        hdr, a = ab.read_fits(p)
        assert a.dtype == np.uint16 and (a == u).all(), "uint16"
        assert hdr["OBJECT"] == "it's M31" and hdr["NAXIS1"] == 7, hdr
        write_fits(p, u[::-1].copy())                             # default row order: stored bottom row first
        assert (ab.read_fits(p)[1] == u).all(), "bottom-up rows"
        f = rng.random((3, 4, 6), dtype=np.float32)
        write_fits(p, f[:, ::-1].copy())
        a = ab.read_fits(p)[1]
        assert a.dtype == np.float32 and np.allclose(a, f), "float colour"
        lp = ab.load(p)
        assert lp.shape == (4, 6, 3) and np.allclose(lp, np.moveaxis(f, 0, -1)), "load() colour"
        i = rng.integers(-32768, 32767, (4, 4), dtype=np.int16)
        write_fits(p, i, ROWORDER="TOP-DOWN")
        assert np.allclose(ab.read_fits(p)[1], i), "signed int16"
        for rows, pat, want in (("TOP-DOWN", "GRBG", "GRBG"), (None, "RGGB", "GBRG")):
            write_fits(p, u[:4, :6].copy(), BAYERPAT=pat, **({"ROWORDER": rows} if rows else {}))
            assert ab.read_raw(p)[1] == want, (rows, pat, ab.read_raw(p)[1])
        write_fits(p, u[:4, :6].copy(), BAYERPAT="RGGB", XBAYROFF=1, ROWORDER="TOP-DOWN")
        assert ab.read_raw(p)[1] == "GRBG"
        with open(p, "wb") as fh: fh.write(b"not a fits file" * 300)
        try: ab.read_fits(p); assert False, "accepted junk"
        except ValueError: pass


# ---------------------------------------------------------------- tests on the real frames
def test_stack_raw():
    with tempfile.TemporaryDirectory() as tmp:
        dirs = subset_dirs(tmp)
        r, m = run_stack(dirs)
        assert r["used"] >= 10, r
        check_against_baseline(m)
        a, stats = ab.prepare(r["file"])
        out = ab.process(a[::4, ::4].copy(), stats=dict(stats), **ab.DEFAULTS)
        assert np.isfinite(out).all() and 0.0 <= out.min() and out.max() <= 1.0


def test_stack_fits_bayer():
    """The same frames as test_stack_raw, as colour FITS files (half stored bottom-up), stack just as well."""
    with tempfile.TemporaryDirectory() as tmp:
        conv = raw_to_fits()
        flip = raw_to_fits(bottom_up=True)
        src = ab.raw_files(DATA["main"])[0]
        want = ab.read_raw(src)
        for c, name in ((conv, "td.fits"), (flip, "bu.fits")):  # reads back as the same mosaic and pattern
            c(src, os.path.join(tmp, name))
            got = ab.read_raw(os.path.join(tmp, name))
            assert got[1] == want[1] and (got[0] == want[0]).all(), name
        n = [0]
        def mixed(src, dst):
            n[0] += 1; (flip if n[0] % 2 else conv)(src, dst)
        r, m = run_stack(subset_dirs(tmp, mixed))
        assert r["used"] >= 10 and not r["mono"], r
        assert m["stars"] >= 0.95 * BASELINE["stars"], m
        assert m["fwhm"] <= 1.03 * BASELINE["fwhm"], m


def test_stack_fits_mono():
    with tempfile.TemporaryDirectory() as tmp:
        r, m = run_stack(subset_dirs(tmp, raw_to_fits(mono=True)))
        assert r["used"] >= 10 and r["mono"], r
        assert tifffile_ndim(r["file"]) == 2
        assert m["stars"] >= 1500 and m["fwhm"] < 3, m      # half the resolution: about half the FWHM
        a, stats = ab.prepare(r["file"])
        assert np.isfinite(ab.process(a[::4, ::4].copy(), stats=dict(stats), **ab.DEFAULTS)).all()


def tifffile_ndim(path):
    import tifffile
    return tifffile.imread(path).ndim


def test_rejects_satellite():
    """A satellite trail in one of a handful of frames: too few for plain kappa-sigma clipping to catch, but
    the winsorized clipping removes it. Compared with the same stack without the trail."""
    global SUBSET
    keep = SUBSET; SUBSET = {"main": 6}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clean = subset_dirs(os.path.join(tmp, "clean"), raw_to_fits(mono=True))
            victim = sorted(os.listdir(clean["main"]))[2]
            trail = subset_dirs(os.path.join(tmp, "trail"),
                                raw_to_fits(mono=True, trail=(victim.replace(".fits", ".ARW"), 400)))
            res = {}
            for rej in ("sigma", "winsorized"):
                ra, rb = ab.stack(clean["main"], rejection=rej), ab.stack(trail["main"], rejection=rej)
                assert victim not in dict(rb["skipped"]), rb["skipped"]
                a, b = ab.load(ra["file"])[..., 0], ab.load(rb["file"])[..., 0]
                (xa, ya), (xb, yb) = ra["origin"], rb["origin"]      # line them up in the reference frame
                x0, y0 = max(xa, xb), max(ya, yb)
                x1, y1 = min(xa + a.shape[1], xb + b.shape[1]), min(ya + a.shape[0], yb + b.shape[0])
                d = b[y0 - yb:y1 - yb, x0 - xb:x1 - xb] - a[y0 - ya:y1 - ya, x0 - xa:x1 - xa]
                d -= np.median(d)
                res[rej] = int((np.abs(d) > 5 * measure_noise(a)).sum())
                print(f"    {rej}: {ra['used']} frames, {res[rej]} pixels changed by the trail")
            assert res["sigma"] > 10000, "the test trail should get through plain sigma clipping"
            # what's left is the trail's soft edges: fainter than one frame's noise, so no per-pixel test sees them
            assert res["winsorized"] < res["sigma"] / 20, res
    finally:
        SUBSET = keep


def measure_noise(L):
    h, w = L.shape
    bg = cv2.resize(cv2.medianBlur(cv2.resize(L, (w // 16, h // 16), interpolation=cv2.INTER_AREA), 5),
                    (w, h), interpolation=cv2.INTER_LINEAR)
    r = (L - bg)[::2, ::2]
    return 1.4826 * float(np.median(np.abs(r - np.median(r))))


TESTS = [test_fits_reader, test_stack_raw, test_stack_fits_bayer, test_stack_fits_mono, test_rejects_satellite]


NEEDS_DATA = {"test_stack_raw", "test_stack_fits_bayer", "test_stack_fits_mono", "test_rejects_satellite"}


def main():
    want = sys.argv[1:]
    failed = 0
    for t in TESTS:
        if want and t.__name__ not in want: continue
        if t.__name__ in NEEDS_DATA and not HAVE_DATA:
            print(f"SKIP {t.__name__} (no test frames)"); continue
        print(f"RUN  {t.__name__}")
        try:
            t(); print(f"ok   {t.__name__}")
        except Exception as e:
            failed += 1; print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
