#!/usr/bin/env python3
"""
astrobake.py - automatic processing of a linear DSS/Siril stack,
with a local web UI (sliders + live preview) or straight from the command line.

Steps: crop dead stacking edges -> remove background gradient -> neutralize sky
-> star-based white balance -> color-preserving arcsinh stretch -> black point
+ midtone stretch -> SCNR green removal -> S-curve -> saturation -> background
noise reduction -> final sky neutralization -> rotate -> crop.

App:      ready-to-run Windows/macOS/Linux downloads, no Python needed - see README.md
Start:    python astrobake.py                          # UI, pick a file in the page
          python astrobake.py stacked.tif              # UI with that file loaded
Batch:    python astrobake.py stacked.tif --batch      # default settings straight to files
          python astrobake.py stacked.tif --batch --stretch 4000 --rotate 12.5
          (the UI shows the exact --batch command for whatever you've dialed in)
Packages: numpy tifffile imagecodecs opencv-python-headless pillow (it offers to install them,
          in a private .venv if the system Python is locked or has no pip)
Outputs:  <name>_processed.jpg next to the input file, plus <name>_processed_16bit.tif
          (for further editing in GIMP) with --tif or the UI's TIFF checkbox.
Needs roughly 6x the image size in RAM (about 2 GB for a 24 MP stack).
"""
import sys, importlib.util, os, shutil, subprocess

FROZEN = getattr(sys, "frozen", False)   # running as the packaged app (PyInstaller), not a script

if sys.argv[1:2] == ["--pick-file"]:
    # helper process for the native file dialog (see pick_file); writes the choice to a file
    # because a packaged app without a console has no usable stdout
    import tkinter as tk
    from tkinter import filedialog
    r = tk.Tk(); r.withdraw()
    try: r.attributes("-topmost", True)
    except Exception: pass
    p = filedialog.askopenfilename(title="Open stacked image", initialdir=sys.argv[2] or None,
                                   filetypes=[("TIFF images", "*.tif *.tiff"), ("All files", "*.*")])
    with open(sys.argv[3], "w", encoding="utf-8") as f: f.write(p or "")
    sys.exit(0)

# ---------------------------------------------------------------- dependency check
REQUIRED = {"numpy": "numpy", "tifffile": "tifffile", "imagecodecs": "imagecodecs",
            "cv2": "opencv-python-headless", "PIL": "pillow"}
_missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
_here = os.path.dirname(os.path.abspath(__file__))
_venv = os.path.join(_here, ".venv")
_venv_py = os.path.join(_venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(_venv, "bin", "python")
if _missing and os.path.exists(_venv_py) and os.path.abspath(sys.prefix) != _venv:
    # packages live in the project's virtualenv - rerun with that Python
    sys.exit(subprocess.call([_venv_py, os.path.abspath(__file__), *sys.argv[1:]]))


def _ask(question):
    if not (sys.stdin and sys.stdin.isatty()): return False
    try: return input(question).strip().lower() in ("", "y", "yes")
    except EOFError: return False


def _system_pip_usable():
    """pip exists and the OS allows installing into this Python (PEP 668 locks many distros' Pythons)."""
    import sysconfig
    if importlib.util.find_spec("pip") is None: return False
    locked = os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"))
    return sys.prefix != sys.base_prefix or not locked


if _missing:
    rerun = subprocess.list2cmdline([os.path.basename(sys.executable), os.path.relpath(__file__)])
    print("AstroBake needs some Python packages that aren't installed:\n    " + ", ".join(_missing))
    if _system_pip_usable():
        cmd = [sys.executable, "-m", "pip", "install", *_missing]
        if _ask("\nInstall them now? [Y/n] "):
            if subprocess.call(cmd) == 0:
                print("\nInstalled. Starting…\n")
                sys.exit(subprocess.call([sys.executable, *sys.argv]))   # restart with packages available
            print("\nInstall failed - see the error above.")
        print("\nTo install manually, run:\n\n    " + subprocess.list2cmdline(cmd) + "\n")
        sys.exit(1)

    # this Python can't take packages directly, so give AstroBake its own virtual environment
    rel = os.path.relpath(_venv)
    print(f"\nAstroBake can install them into its own virtual environment ({rel}),"
          " leaving your system Python untouched.")
    if not _ask("Set it up now? [Y/n] "):
        print(f"\nRun {rerun} again in a terminal and answer Y to set it up.\n")
        sys.exit(1)
    existed = os.path.exists(_venv)
    if subprocess.call([sys.executable, "-m", "venv", _venv]) != 0:
        if not existed: shutil.rmtree(_venv, ignore_errors=True)   # don't leave a half-made venv behind
        print("\nCouldn't create the virtual environment: Python's venv support isn't installed.")
        if shutil.which("apt"):
            print("Install it with:\n\n    sudo apt install python3-venv\n")
        else:
            print("Install your system's python3-venv package (or equivalent).\n")
        print(f"Then run AstroBake again:\n\n    {rerun}\n")
        sys.exit(1)
    if subprocess.call([_venv_py, "-m", "pip", "install", *_missing]) != 0:
        print(f"\nInstall failed - see the error above. Once it's fixed, run AstroBake again:\n\n    {rerun}\n")
        sys.exit(1)
    print("\nInstalled. Starting…\n")
    sys.exit(subprocess.call([_venv_py, os.path.abspath(__file__), *sys.argv[1:]]))

import argparse, gc, io, json, math, tempfile, threading, time, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import numpy as np, tifffile, cv2
from PIL import Image


# ---------------------------------------------------------------- loading / linear prep
def load(path):
    try:
        a = tifffile.imread(path)
    except Exception as e:
        if "imagecodecs" in str(e):
            print("This TIFF is compressed (LZW/ZIP) and needs one more package:\n\n    "
                  + sys.executable + " -m pip install imagecodecs\n")
            sys.exit(1)
        raise
    if a.ndim == 2:
        a = np.stack([a] * 3, -1)                     # mono stack -> gray RGB
    if a.ndim == 3 and a.shape[0] in (3, 4) and a.shape[-1] not in (3, 4):
        a = np.moveaxis(a, 0, -1)                     # planar -> interleaved
    a = a[..., :3]
    if np.issubdtype(a.dtype, np.integer):
        a = a.astype(np.float32) / np.iinfo(a.dtype).max
    else:
        a = a.astype(np.float32)
        a /= max(1.0, float(a.max()))
    return a


def autocrop(a, margin=20):
    """Remove rows/cols at the borders that contain empty (zero) stacking pixels."""
    z = a.sum(2) <= 0
    H, W = z.shape
    t, b, l, r = 0, H, 0, W
    # repeatedly shave whichever border is "most empty" until no border has empty pixels
    while b - t > H // 3 and r - l > W // 3:
        edges = {"t": z[t, l:r].mean(), "b": z[b - 1, l:r].mean(),
                 "l": z[t:b, l].mean(), "r": z[t:b, r - 1].mean()}
        side = max(edges, key=edges.get)
        if edges[side] <= 0.0005: break
        if side == "t": t += 1
        elif side == "b": b -= 1
        elif side == "l": l += 1
        else: r -= 1
    t, b, l, r = t + margin, b - margin, l + margin, r - margin
    print(f"crop: rows {t}:{b}, cols {l}:{r} (from {H}x{W})")
    return a[t:b, l:r].copy()


def remove_gradient(a, order=2):
    """Fit a low-order 2D polynomial to sky-only samples and subtract it per channel."""
    H, W, _ = a.shape
    sm = cv2.resize(a, (W // 16, H // 16), interpolation=cv2.INTER_AREA)
    L = sm.mean(2)
    med = np.median(L); mad = 1.4826 * np.median(abs(L - med))
    mask = cv2.medianBlur(L, 5) < med + 2 * mad          # reject galaxy / stars
    mask = cv2.erode(mask.astype(np.uint8), np.ones((13, 13))) > 0  # stay off halos
    yy, xx = np.nonzero(mask)
    Y, X = yy / sm.shape[0] - .5, xx / sm.shape[1] - .5
    pw = [(i, j) for i in range(order + 1) for j in range(order + 1 - i)]
    A = np.stack([X**i * Y**j for i, j in pw], 1)
    gx = (np.arange(W, dtype=np.float32) / W - .5)[None, :]
    gy = (np.arange(H, dtype=np.float32) / H - .5)[:, None]
    for c in range(3):
        v = cv2.medianBlur(sm[..., c], 5)[yy, xx]
        coef = np.linalg.lstsq(A, v, rcond=None)[0]
        for _ in range(3):                                  # sigma-clip outliers
            r = v - A @ coef; k = abs(r) < 3 * r.std()
            coef = np.linalg.lstsq(A[k], v[k], rcond=None)[0]
        bg = sum(k * gx**i * gy**j for k, (i, j) in zip(coef, pw)).astype(np.float32)
        a[..., c] -= bg; del bg
        a[..., c] -= np.median(a[::4, ::4, c])              # sky -> 0, equal per channel
    return a


def white_balance(a):
    """Scale channels so moderately bright pixels (mostly stars) average neutral."""
    s = a[::2, ::2]; L = s.mean(2)
    sel = (L > np.percentile(L, 99)) & (L < np.percentile(L, 99.95))
    k = [s[..., 1][sel].mean() / s[..., c][sel].mean() for c in range(3)]
    print("white balance R,G,B:", [round(float(x), 3) for x in k])
    for c in range(3): a[..., c] *= k[c]
    return a


def prepare(path, crop=True, progress=None):
    """Load + all the linear steps that don't depend on slider settings. Returns (image, stats).
    `progress(step, img=None)` hears about each step, and gets the stack as loaded (still linear)."""
    say = progress or (lambda step, img=None: None)
    say("Reading file…")
    a = load(path)
    if crop:
        say("Cropping stacking edges…")
        a = autocrop(a)
    say("Removing background gradient…", a)
    a = remove_gradient(a)
    say("Balancing star colors…")
    a = white_balance(a)
    noise = float(1.4826 * np.median(abs(a[::4, ::4, 1])))
    amax = float(a.max()) + 2.5 * noise
    return a, {"noise": noise, "amax": amax}


# ---------------------------------------------------------------- non-linear processing
def mtf(x, m, target):
    """Midtone transfer: maps median m to target (same as PixInsight/Siril autostretch)."""
    mt = m * (target - 1) / ((2 * target - 1) * m - target)
    return ((mt - 1) * x) / ((2 * mt - 1) * x - mt)


# Default slider values ("natural"). Every look is reachable from the sliders in the UI.
DEFAULTS = dict(stretch=900, target=0.13, sat=1.7, sky=0.075, denoise=1.3, scurve=0.12, sharpen=0.0, temp=0.0)
PARAMS = tuple(DEFAULTS)


def process(a, stretch=900., target=0.13, sat=1.7, sky=0.075, denoise=1.3, scurve=0.12, sharpen=0., temp=0.,
            stats=None, px=1.0):
    """Non-linear processing, in place where possible.

    `stats` holds image-wide numbers (noise level, black point, medians...). Any that are
    present are used as-is, missing ones are measured from `a` and written back. That lets
    a small preview or a 1:1 crop be processed exactly like the full image would be.
    `px` is the size of one pixel in full-resolution pixels, so blur radii match on a downscaled preview.
    """
    st = stats if stats is not None else {}
    if "noise" not in st: st["noise"] = float(1.4826 * np.median(abs(a[::4, ::4, 1])))
    a += 2.5 * st["noise"]; np.clip(a, 0, None, out=a)
    if "amax" not in st: st["amax"] = float(a.max())
    a /= st["amax"]

    # color-preserving arcsinh stretch: stretch luminance, keep RGB ratios.
    # Higher `stretch` = more highlight compression (core keeps detail) AND brighter faint stuff.
    L = a.mean(2) + 1e-9
    f = (np.arcsinh(stretch * L) / np.arcsinh(stretch)) / L; del L
    a *= f[..., None]; del f; gc.collect()
    if "hi" not in st: st["hi"] = max(1.0, float(np.percentile(a[::4, ::4], 99.995)))
    a /= st["hi"]; np.clip(a, 0, 1, out=a)

    # black point just under the sky, then midtone stretch
    if "bp" not in st:
        L = a[::4, ::4].mean(2); m0 = np.median(L)
        st["bp"] = float(m0 - 2.8 * 1.4826 * np.median(abs(L - m0)))
    a -= st["bp"]; a /= (1 - st["bp"]); np.clip(a, 0, 1, out=a)
    if "mid" not in st: st["mid"] = float(np.median(a[::4, ::4]))
    a[:] = mtf(a, st["mid"], target)

    np.minimum(a[..., 1], (a[..., 0] + a[..., 2]) / 2, out=a[..., 1])   # SCNR green
    a += scurve * a * (1 - a) * (2 * a - 1); np.clip(a, 0, 1, out=a)     # S-curve
    L = a.mean(2, keepdims=True); a -= L; a *= sat; a += L; del L         # saturation
    np.clip(a, 0, 1, out=a)

    if sharpen > 0.01:   # unsharp mask on the brighter structure only, so background noise isn't amplified
        bl = cv2.GaussianBlur(a, (0, 0), 2.0 / px)
        w = np.clip((a.mean(2) - target - 0.1) / 0.2, 0, 1)[..., None]   # the sky sits near `target` here
        a += sharpen * w * (a - bl); np.clip(a, 0, 1, out=a); del bl, w; gc.collect()

    if denoise > 0.05:   # blur only faint areas so galaxy/stars stay sharp
        bl = cv2.GaussianBlur(a, (0, 0), denoise / px)
        w = np.clip((a.mean(2) - 0.18) / 0.15, 0, 1)[..., None]
        a = bl * (1 - w) + a * w; del bl, w; gc.collect()

    if "skymed" not in st: st["skymed"] = [float(x) for x in np.median(a[::8, ::8], axis=(0, 1))]
    for c in range(3):                           # final neutral sky at level `sky`
        m = st["skymed"][c]
        a[..., c] = np.clip((a[..., c] - m) * (1 - sky) / (1 - m) + sky, 0, 1)
    if abs(temp) > 0.001:                        # warm/cool the object, pivoting on the sky so it stays neutral
        for c, g in ((0, 1 + 0.15 * temp), (2, 1 - 0.15 * temp)):
            a[..., c] = np.clip((a[..., c] - sky) * g + sky, 0, 1)
    return a


# ---------------------------------------------------------------- rotation / crop
# Angles are in degrees, positive = clockwise. The rotated image sits on an expanded canvas
# (so nothing is cut off); crops are normalized (x0, y0, x1, y1) on that canvas.
def rot_geom(W, H, angle):
    """Affine matrix (source -> rotated canvas) and canvas size, for a W x H image."""
    # pixel-centre convention so 90/180 degree turns map pixels exactly
    M = cv2.getRotationMatrix2D(((W - 1) / 2, (H - 1) / 2), -angle, 1.0)   # OpenCV: + = counter-clockwise
    c, s = abs(M[0, 0]), abs(M[0, 1])
    W2, H2 = int(round(W * c + H * s)), int(round(W * s + H * c))
    M[0, 2] += (W2 - 1) / 2 - (W - 1) / 2; M[1, 2] += (H2 - 1) / 2 - (H - 1) / 2
    return M, W2, H2


def inscribed_crop(W, H, angle):
    """Largest axis-aligned crop with no empty corners after rotating, normalized."""
    t = math.radians(angle)
    sin_a, cos_a = abs(math.sin(t)), abs(math.cos(t))
    long_, short = (W, H) if W >= H else (H, W)
    if short <= 2 * sin_a * cos_a * long_ or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * short
        wr, hr = (x / sin_a, x / cos_a) if W >= H else (x / cos_a, x / sin_a)
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr, hr = (W * cos_a - H * sin_a) / cos_2a, (H * cos_a - W * sin_a) / cos_2a
    _, W2, H2 = rot_geom(W, H, angle)
    pad = 0 if abs(angle) % 90 < 1e-9 else 2          # stay clear of anti-aliased edges
    wr, hr = max(1, wr - pad), max(1, hr - pad)
    return ((W2 - wr) / 2 / W2, (H2 - hr) / 2 / H2, (W2 + wr) / 2 / W2, (H2 + hr) / 2 / H2)


def rotate(a, angle, quality=False):
    q = angle / 90
    if abs(q - round(q)) < 1e-9:                    # exact, lossless quarter turns
        k = int(round(q)) % 4
        return a if k == 0 else np.ascontiguousarray(np.rot90(a, -k))
    M, W2, H2 = rot_geom(a.shape[1], a.shape[0], angle)
    r = cv2.warpAffine(a, M, (W2, H2), flags=cv2.INTER_CUBIC if quality else cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return np.clip(r, 0, 1, out=r)


def apply_crop(a, crop):
    if not crop: return a
    H, W = a.shape[:2]
    x0, y0, x1, y1 = (int(round(v * s)) for v, s in zip(crop, (W, H, W, H)))
    x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(W, max(x0 + 1, x1)), min(H, max(y0 + 1, y1))
    return a[y0:y1, x0:x1]


# ---------------------------------------------------------------- output helpers
def save(a, out, tif=False):
    written = []
    if tif:
        tifffile.imwrite(out + "_16bit.tif", (a * 65535 + .5).astype(np.uint16), compression="zlib")
        written.append(out + "_16bit.tif")
    Image.fromarray((a * 255 + .5).astype(np.uint8)).save(out + ".jpg", quality=93)
    written.append(out + ".jpg")
    print("wrote", " + ".join(written))
    return written


def jpeg_bytes(a, q=90):
    buf = io.BytesIO()
    Image.fromarray((np.clip(a, 0, 1) * 255 + .5).astype(np.uint8)).save(buf, "JPEG", quality=q)
    return buf.getvalue()


def render_full(full, base_stats, settings, angle=0.0, crop=None):
    """Full-resolution result: process -> rotate -> crop. Used by both export and --batch."""
    a = process(full.copy(), stats=dict(base_stats), **settings)
    a = rotate(a, angle, quality=True)
    if crop is None and abs(angle) % 90 > 1e-6:
        crop = inscribed_crop(full.shape[1], full.shape[0], angle)
    return apply_crop(a, crop)


# ---------------------------------------------------------------- web UI
UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AstroBake</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#111111;--panel:#1a1a1a;--line:#2e2e2e;--text:#ececec;--dim:#9a9a9a;--acc:#e4e4e4;--warn:#ff7a59;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Roboto,system-ui,-apple-system,Segoe UI,sans-serif;
     display:grid;grid-template-columns:310px 1fr;height:100vh;overflow:hidden}
aside{background:var(--panel);border-right:1px solid var(--line);padding:18px;overflow:auto}
h1{font-size:16px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}
h1 button{font-size:11px;font-weight:400;padding:3px 8px}
h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:20px 0 10px;
   display:flex;justify-content:space-between;align-items:center;font-weight:600}
h2 button{font-size:11px;padding:3px 8px;letter-spacing:0;text-transform:none}
.filebar{display:flex;gap:8px;align-items:center}
.file{color:var(--dim);font-size:12px;word-break:break-all;flex:1}
.file small{display:block;font-size:11px;opacity:.75;margin-top:1px}
button{background:#242424;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 10px;
       cursor:pointer;font:inherit}
button:hover{border-color:var(--acc)}
button:disabled{opacity:.5;cursor:default}
button.primary{background:var(--acc);color:#111111;border:none;font-weight:600;padding:9px 14px}
.ctl{margin-bottom:14px}
.ctl label{display:flex;justify-content:space-between;font-size:13px}
.ctl label span{color:var(--acc);font-variant-numeric:tabular-nums}
.ctl small{color:var(--dim);font-size:11.5px;display:block;margin-top:2px}
input[type=range]{width:100%;accent-color:var(--acc)}
input[type=text],input[type=number]{background:#111111;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
input[type=text]{width:100%}
.rotrow{display:flex;gap:6px;align-items:center;margin-top:6px}
.rotrow input[type=number]{width:84px}
.rotrow button{padding:6px 9px}
.note{font-size:11.5px;color:var(--dim)}
.export{border-top:1px solid var(--line);padding-top:14px;margin-top:10px}
.export .primary{width:100%}
.export label{font-size:13px;display:block;margin:0 0 8px}
#msg{font-size:12px;color:var(--dim);margin-top:8px;word-break:break-all}
code{display:block;background:#111111;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:11px;
     color:var(--dim);margin-top:10px;word-break:break-all;user-select:all}
main{display:grid;grid-template-rows:58% 42%;min-height:0;position:relative}
.view{position:relative;display:flex;align-items:center;justify-content:center;min-height:0;padding:12px}
#stage{position:relative;overflow:hidden;line-height:0;touch-action:none;user-select:none}
#stage img{display:block;max-width:calc(100vw - 340px);max-height:calc(58vh - 24px)}
#cropbox{position:absolute;border:1.5px solid var(--acc);box-shadow:0 0 0 9999px rgba(0,0,0,.62);pointer-events:none}
#cropbox .g{position:absolute;inset:0;display:none}
#cropbox .g::before,#cropbox .g::after{content:"";position:absolute}
#cropbox .thirds::before{inset:33.33% 0;border-top:1px solid #ffffff40;border-bottom:1px solid #ffffff40}
#cropbox .thirds::after{inset:0 33.33%;border-left:1px solid #ffffff40;border-right:1px solid #ffffff40}
#cropbox .center::before{left:0;right:0;top:50%;border-top:1px dashed #ffffff80}
#cropbox .center::after{top:0;bottom:0;left:50%;border-left:1px dashed #ffffff80}
#cropbox[data-g=thirds] .thirds,#cropbox[data-g=both] .thirds,
#cropbox[data-g=center] .center,#cropbox[data-g=both] .center{display:block}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:none;border-radius:0;padding:4px 9px;font-size:12px;background:transparent;color:var(--dim)}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on{background:var(--acc);color:#111111;font-weight:600}
.guiderow{display:flex;justify-content:space-between;align-items:center;font-size:13px;margin-top:10px}
#marker{position:absolute;border:1px dashed #ffffffc0;background:#ffffff0d;pointer-events:none;display:none}
.pane.pan{cursor:grab;touch-action:none;user-select:none}.pane.pan.grabbing{cursor:grabbing}
#det{will-change:transform}
.bottom{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line);min-height:0}
.pane{position:relative;display:flex;align-items:center;justify-content:center;min-height:0;min-width:0;padding:10px;overflow:hidden}
.pane+.pane{border-left:1px solid var(--line)}
.pane img{max-width:100%;max-height:100%}
.pane>img,.pane>canvas{transition:opacity .6s}
#res{display:block}
#stage #orig{position:absolute;inset:0;width:100%;height:100%;max-width:none;max-height:none;z-index:3;display:none}
#sweep{position:absolute;top:0;bottom:0;left:0;width:2px;margin-left:-1px;background:#ffffff;
       box-shadow:0 0 16px 5px #ffffff90;z-index:4;display:none}
#steps{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);background:#111111d9;border:1px solid var(--line);
       font-size:13px;padding:7px 14px;border-radius:999px;z-index:6;display:none;white-space:nowrap}
#steps::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--acc);
               margin-right:9px;vertical-align:1px;animation:pulse 1s ease-in-out infinite alternate}
@keyframes pulse{from{opacity:.25}to{opacity:1}}
body.intro #cropbox,body.intro #marker{display:none!important}
body.intro #stage{pointer-events:none}
body.intro .pane>:not(.tag){opacity:0}
.tag{position:absolute;top:8px;left:14px;font-size:12px;color:var(--dim);background:#111111cc;padding:3px 8px;border-radius:4px;z-index:2}
#busy{position:absolute;top:8px;right:14px;font-size:12px;color:var(--text);background:#111111d9;border:1px solid var(--line);
      padding:4px 10px 4px 8px;border-radius:999px;z-index:6;display:flex;align-items:center;gap:7px;
      opacity:0;transition:opacity .15s;pointer-events:none}
#busy::before{content:"";width:11px;height:11px;border-radius:50%;border:2px solid #ffffff30;border-top-color:var(--acc);
              animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
body.rendering #busy{opacity:1;transition-delay:.12s}
main::before{content:"";position:absolute;top:0;left:0;height:2px;width:30%;background:linear-gradient(90deg,transparent,var(--acc),transparent);
             z-index:7;opacity:0;transition:opacity .2s;pointer-events:none;animation:scan 1.1s ease-in-out infinite}
body.rendering main::before{opacity:1;transition-delay:.12s}
@keyframes scan{from{left:-30%}to{left:100%}}
#welcome{position:absolute;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;
         justify-content:center;gap:14px;text-align:center;padding:30px;z-index:5}
#welcome h2{margin:0;font-size:20px;font-weight:600;color:var(--text);text-transform:none;letter-spacing:0}
#welcome p{margin:0;color:var(--dim);max-width:460px}
#welcome.loading p,#welcome.loading .primary{display:none}
#welcome.loading h2{font-size:16px;font-weight:500;display:flex;align-items:center;gap:10px}
#welcome.loading h2::before{content:"";width:14px;height:14px;border-radius:50%;border:2px solid #ffffff30;
                            border-top-color:var(--acc);animation:spin .7s linear infinite}
body.dropping main::after{content:"Drop to open";position:absolute;inset:12px;border:2px dashed var(--acc);border-radius:10px;
     background:#111111e6;color:var(--acc);font-size:18px;display:flex;align-items:center;justify-content:center;z-index:10}
.ctlwrap.off{opacity:.4;pointer-events:none}
</style></head><body>
<aside>
  <h1>AstroBake <button id="quit" title="Stop AstroBake">Quit</button></h1>
  <div class="filebar"><div class="file" id="file">No image loaded</div><button id="open2">Open…</button></div>
  <div class="ctlwrap off" id="ctlwrap">
    <h2>Rotate &amp; crop</h2>
    <div class="ctl">
      <label>Rotation<span id="v_angle">0.0°</span></label>
      <input type="range" id="s_angle" min="-180" max="180" step="0.1" value="0">
      <div class="rotrow">
        <button id="r_m90" title="Rotate 90° counter-clockwise">↺ 90°</button>
        <button id="r_p90" title="Rotate 90° clockwise">↻ 90°</button>
        <input type="number" id="n_angle" step="0.1" min="-180" max="180" value="0" title="Exact angle in degrees">
        <button id="r_0">0°</button>
      </div>
    </div>
    <div class="ctl">
      <label>Crop<span id="v_crop"></span></label>
      <div id="w_crop" class="note" style="color:var(--warn);display:none;margin:2px 0 4px">⚠ Crop includes empty (black) corners</div>
      <div class="rotrow"><button id="c_reset">Reset crop</button></div>
      <div class="guiderow">Guides<span class="seg" id="guides"><button data-g="thirds">Thirds</button><button data-g="center">Center</button><button data-g="both">Both</button><button data-g="none">None</button></span></div>
    </div>
    <h2>Tone &amp; color <button id="t_reset">Reset</button></h2>
    <div id="ctls"></div>
    <div class="export">
      <label><input type="checkbox" id="tif"> Also save 16-bit TIFF</label>
      <button class="primary" id="exp">Export full resolution</button>
      <div id="msg"></div>
      <code id="cmd"></code>
    </div>
  </div>
</aside>
<main>
  <div id="welcome">
    <h2 id="wtitle">Open a stacked image</h2>
    <p id="wtext">Pick the TIFF straight out of DeepSkyStacker or Siril (16- or 32-bit), or drag it onto this page. The first load takes
      a few seconds while it crops the dead stacking edges, removes the gradient and balances color.</p>
    <button class="primary" id="open1">Open stack…</button>
  </div>
  <div class="view"><span class="tag">Preview: adjust crop · drag the dashed box to move the 1:1 detail</span><span id="busy">Rendering…</span>
    <div id="stage"><img id="prev" alt="" draggable="false"><img id="orig" alt="" draggable="false"><div id="sweep"></div>
      <div id="cropbox" data-g="thirds"><div class="g thirds"></div><div class="g center"></div></div><div id="marker"></div></div><div id="steps"></div></div>
  <div class="bottom">
    <div class="pane"><span class="tag">Result</span><canvas id="res"></canvas></div>
    <div class="pane pan" id="detpane"><span class="tag">1:1 detail (actual pixels) · drag to pan</span><img id="det" alt="" draggable="false"></div>
  </div>
</main>
<script>
const CTL = [
 {k:"stretch", name:"Stretch strength", min:50, max:10000, log:true},
 {k:"target", name:"Brightness", min:0.05, max:0.35, step:0.005},
 {k:"sky", name:"Sky background level", min:0, max:0.2, step:0.005},
 {k:"scurve", name:"Contrast (S-curve)", min:0, max:0.35, step:0.01},
 {k:"sharpen", name:"Sharpening", min:0, max:2, step:0.05},
 {k:"sat", name:"Saturation", min:0.5, max:3, step:0.05},
 {k:"temp", name:"Color temperature", min:-1, max:1, step:0.05},
 {k:"denoise", name:"Background noise reduction", min:0, max:4, step:0.1},
];
let P = {}, DEF = {}, A = 0, C = [0,0,1,1], W = 0, H = 0, W2 = 0, H2 = 0, DW = 1100, DH = 700;
let cx = 0.5, cy = 0.5, busy = false, dirty = false, built = false, loaded = false, drag = null;
const $ = id => document.getElementById(id);
const clamp = (v,a,b) => Math.min(b, Math.max(a, v));
const toSlider = (c,v) => c.log ? Math.log10(v) : v;
const fromSlider = (c,v) => c.log ? Math.round(10**v) : +v;
const fmt = (c,v) => c.log ? String(v) : (+v).toFixed(c.step < 0.01 ? 3 : 2);

/* ---------- geometry (mirrors rot_geom / inscribed_crop in Python) ---------- */
function geom(){
  const t = A*Math.PI/180, c = Math.abs(Math.cos(t)), s = Math.abs(Math.sin(t));
  W2 = Math.round(W*c + H*s); H2 = Math.round(W*s + H*c);
}
function inscribed(){
  const t = A*Math.PI/180, sa = Math.abs(Math.sin(t)), ca = Math.abs(Math.cos(t));
  const wl = W >= H, lng = wl ? W : H, sht = wl ? H : W;
  let wr, hr;
  if (sht <= 2*sa*ca*lng || Math.abs(sa-ca) < 1e-10){
    const x = 0.5*sht; [wr,hr] = wl ? [x/sa, x/ca] : [x/ca, x/sa];
  } else {
    const c2 = ca*ca - sa*sa; wr = (W*ca - H*sa)/c2; hr = (H*ca - W*sa)/c2;
  }
  const pad = (Math.abs(A) % 90 < 1e-9) ? 0 : 2;
  wr = Math.max(1, wr-pad); hr = Math.max(1, hr-pad);
  return [(W2-wr)/2/W2, (H2-hr)/2/H2, (W2+wr)/2/W2, (H2+hr)/2/H2];
}
function cropInside(){
  // map each crop corner back into the un-rotated image and check it lands on real pixels
  const th = -A*Math.PI/180, c = Math.cos(th), sn = Math.sin(th), tol = 2;
  return [[C[0],C[1]],[C[2],C[1]],[C[0],C[3]],[C[2],C[3]]].every(([x,y]) => {
    const u = x*W2 - W2/2, v = y*H2 - H2/2;
    const sx = c*u - sn*v + W/2, sy = sn*u + c*v + H/2;
    return sx > -tol && sx < W+tol && sy > -tol && sy < H+tol;
  });
}
function drawCrop(){
  const b = $("cropbox");
  b.style.left = C[0]*100+"%"; b.style.top = C[1]*100+"%";
  b.style.width = (C[2]-C[0])*100+"%"; b.style.height = (C[3]-C[1])*100+"%";
  const ok = cropInside();
  $("v_crop").textContent = Math.round((C[2]-C[0])*W2) + " × " + Math.round((C[3]-C[1])*H2) + " px";
  $("w_crop").style.display = ok ? "none" : "block";
  b.style.borderColor = ok ? "" : "#ff7a59";
  updateCmd(); drawResult();
}
function markerRect(){             // the 1:1 detail window, in preview fractions
  const w = Math.min(1, DW/W2), h = Math.min(1, DH/H2);
  const x = clamp(cx - w/2, 0, 1-w), y = clamp(cy - h/2, 0, 1-h);
  return [x, y, x+w, y+h];
}
function setSpot(x, y){            // keep the centre where the window actually sits, so drags don't build up slack
  const w = Math.min(1, DW/W2), h = Math.min(1, DH/H2);
  cx = clamp(x, w/2, 1-w/2); cy = clamp(y, h/2, 1-h/2);
  drawMarker(); shiftDetail(); requestDetail();
}
function drawMarker(){
  const m = $("marker"); if (!$("prev").naturalWidth) return;
  const [x0,y0,x1,y1] = markerRect();
  m.style.display = "block";
  m.style.left = x0*100+"%"; m.style.top = y0*100+"%"; m.style.width = (x1-x0)*100+"%"; m.style.height = (y1-y0)*100+"%";
}
function setAngle(v, keepCrop){
  v = ((+v + 180) % 360 + 360) % 360 - 180;            // wrap to [-180, 180)
  if (Math.abs(v + 180) < 1e-9 && +v > 0) v = 180;
  A = Math.round(v*10)/10;
  $("s_angle").value = A; $("n_angle").value = A; $("v_angle").textContent = A.toFixed(1) + "°";
  geom(); if (!keepCrop) C = inscribed(); drawCrop(); request();
}

/* ---------- tone controls ---------- */
function buildControls(){
  $("ctls").innerHTML = CTL.map(c => `<div class="ctl"><label>${c.name}<span id="v_${c.k}"></span></label>
    <input type="range" id="s_${c.k}" min="${toSlider(c,c.min)}" max="${toSlider(c,c.max)}" step="${c.log?0.01:c.step}">
    ${c.help?`<small>${c.help}</small>`:""}</div>`).join("");
  CTL.forEach(c => $("s_"+c.k).addEventListener("input", e => {
    P[c.k] = fromSlider(c, e.target.value); $("v_"+c.k).textContent = fmt(c,P[c.k]);
    updateCmd(); request(); }));
  built = true;
}
function setTone(vals){
  P = {...vals};
  CTL.forEach(c => { $("s_"+c.k).value = toSlider(c,P[c.k]); $("v_"+c.k).textContent = fmt(c,P[c.k]); });
  updateCmd(); request();
}
function updateCmd(){
  if (!loaded) return;
  let s = `python astrobake.py "${window.FILE}" --batch ` + CTL.map(c => `--${c.k} ${P[c.k]}`).join(" ");
  if (A) s += ` --rotate ${A}`;
  if ($("tif").checked) s += " --tif";
  if (C.some((v,i) => Math.abs(v - [0,0,1,1][i]) > 1e-4)) s += ` --crop ${C.map(v => v.toFixed(4)).join(",")}`;
  $("cmd").textContent = s;
}
const qs = extra => new URLSearchParams({...P, angle:A, cx, cy, dw:DW, dh:DH, ...(extra||{})}).toString();

/* ---------- rendering ---------- */
async function request(){
  if (!loaded) return;
  if (busy){ dirty = true; return; }
  busy = true; dirty = false; rendering(1);
  try{
    const q = qs(), at = [cx, cy];
    const [a,b] = await Promise.all([fetch("/preview?"+q), fetch("/detail?"+q)]);
    if (a.ok){ const u = URL.createObjectURL(await a.blob()); intro && !intro.revealing ? reveal(u) : ($("prev").src = u); }
    if (b.ok) await showDetail(b, at);
  } catch(e){ $("msg").textContent = "Error: " + e; }
  busy = false; rendering(-1);
  if (dirty) request();
}
let pending = 0;
function rendering(d){
  pending += d;
  document.body.classList.toggle("rendering", pending > 0 && !intro);
}
let detAt = null, detBusy = false, detDirty = false;
async function showDetail(resp, at){
  const d = $("det"); d.src = URL.createObjectURL(await resp.blob());
  await d.decode().catch(() => {});
  detAt = at; shiftDetail();
}
function shiftDetail(){            // slide the last render by how far the spot has moved since, until the new one arrives
  const d = $("det");
  if (!detAt || !d.naturalWidth){ d.style.transform = ""; return; }
  const w = Math.min(1, DW/W2), h = Math.min(1, DH/H2), k = d.clientWidth / d.naturalWidth;
  const ex = v => clamp(v, w/2, 1-w/2), ey = v => clamp(v, h/2, 1-h/2);
  const dx = (ex(detAt[0]) - ex(cx))*W2*k, dy = (ey(detAt[1]) - ey(cy))*H2*k;
  d.style.transform = dx || dy ? `translate(${dx}px,${dy}px)` : "";
}
async function requestDetail(){
  if (!loaded) return;
  if (detBusy){ detDirty = true; return; }
  detBusy = true; detDirty = false; rendering(1);
  try{
    const at = [cx, cy], b = await fetch("/detail?"+qs());
    if (b.ok) await showDetail(b, at);
  } catch(e){}
  detBusy = false; rendering(-1);
  if (detDirty) requestDetail();
}
$("prev").addEventListener("load", () => { drawMarker(); drawResult(); });

/* ---------- bottom panes: clean cropped result, and a 1:1 detail sized to its pane ---------- */
function drawResult(){
  const img = $("prev"), cv = $("res"), p = cv.parentElement;
  if (!img.naturalWidth) return;
  const nw = img.naturalWidth, nh = img.naturalHeight;
  const sx = C[0]*nw, sy = C[1]*nh, sw = Math.max(1, (C[2]-C[0])*nw), sh = Math.max(1, (C[3]-C[1])*nh);
  const k = Math.min((p.clientWidth - 20)/sw, (p.clientHeight - 20)/sh), r = devicePixelRatio || 1;
  const w = Math.max(1, Math.floor(sw*k)), h = Math.max(1, Math.floor(sh*k));
  cv.width = Math.round(w*r); cv.height = Math.round(h*r); cv.style.width = w+"px"; cv.style.height = h+"px";
  const g = cv.getContext("2d"); g.imageSmoothingQuality = "high";
  g.drawImage(img, sx, sy, sw, sh, 0, 0, cv.width, cv.height);
}
function detailSize(){
  const p = $("det").parentElement;
  DW = Math.max(64, p.clientWidth - 20); DH = Math.max(64, p.clientHeight - 20);
}
let resizeT;
addEventListener("resize", () => {
  if (!loaded) return;
  detailSize(); drawResult(); drawMarker();
  clearTimeout(resizeT); resizeT = setTimeout(requestDetail, 250);
});

/* ---------- crop box interaction ---------- */
const stage = $("stage"), MIN = 0.03;
const CURSOR = {n:"ns-resize",s:"ns-resize",e:"ew-resize",w:"ew-resize",ne:"nesw-resize",sw:"nesw-resize",
                nw:"nwse-resize",se:"nwse-resize",move:"move",spot:"grab","":"default"};
function rel(e){ const r = stage.getBoundingClientRect();
  return {x:(e.clientX-r.left)/r.width, y:(e.clientY-r.top)/r.height, w:r.width, h:r.height}; }
function hit(p){
  const tx = 10/p.w, ty = 10/p.h, [x0,y0,x1,y1] = C;
  const inX = p.x > x0-tx && p.x < x1+tx, inY = p.y > y0-ty && p.y < y1+ty;
  let m = "";
  if (inX && Math.abs(p.y-y0) < ty) m += "n"; else if (inX && Math.abs(p.y-y1) < ty) m += "s";
  if (inY && Math.abs(p.x-x0) < tx) m += "w"; else if (inY && Math.abs(p.x-x1) < tx) m += "e";
  if (!m){
    const [mx0,my0,mx1,my1] = markerRect();
    if ($("marker").style.display === "block" && p.x > mx0 && p.x < mx1 && p.y > my0 && p.y < my1) m = "spot";
    else if (p.x > x0 && p.x < x1 && p.y > y0 && p.y < y1) m = "move";
  }
  return m;
}
stage.addEventListener("pointerdown", e => {
  if (!loaded) return;
  const p = rel(e); drag = {mode: hit(p), p0: p, C0: [...C], s0: [cx, cy], moved: false};
  if (drag.mode === "spot") stage.style.cursor = "grabbing";
  stage.setPointerCapture(e.pointerId);
});
stage.addEventListener("pointermove", e => {
  const p = rel(e);
  if (!drag){ stage.style.cursor = CURSOR[hit(p)]; return; }
  const dx = p.x - drag.p0.x, dy = p.y - drag.p0.y;
  if (!drag.moved && Math.hypot(dx*p.w, dy*p.h) < 4) return;
  drag.moved = true;
  let [x0,y0,x1,y1] = drag.C0; const m = drag.mode;
  if (m === "spot") return setSpot(drag.s0[0] + dx, drag.s0[1] + dy);
  if (m === "move"){
    const w = x1-x0, h = y1-y0;
    x0 = clamp(x0+dx, 0, 1-w); y0 = clamp(y0+dy, 0, 1-h); x1 = x0+w; y1 = y0+h;
  } else if (m){
    if (m.includes("n")) y0 = clamp(y0+dy, 0, y1-MIN);
    if (m.includes("s")) y1 = clamp(y1+dy, y0+MIN, 1);
    if (m.includes("w")) x0 = clamp(x0+dx, 0, x1-MIN);
    if (m.includes("e")) x1 = clamp(x1+dx, x0+MIN, 1);
  } else return;
  C = [x0,y0,x1,y1]; drawCrop();
});
stage.addEventListener("pointerup", e => {
  if (drag?.mode === "spot") stage.style.cursor = CURSOR.spot;
  drag = null;
});

/* ---------- pan the 1:1 detail by dragging it directly ---------- */
const detpane = $("detpane"); let pan = null;
detpane.addEventListener("pointerdown", e => {
  if (!loaded || !$("det").naturalWidth) return;
  pan = {x: e.clientX, y: e.clientY, s0: [cx, cy], k: $("det").clientWidth / $("det").naturalWidth};
  detpane.setPointerCapture(e.pointerId); detpane.classList.add("grabbing");
});
detpane.addEventListener("pointermove", e => {
  if (!pan) return;
  setSpot(pan.s0[0] - (e.clientX - pan.x)/pan.k/W2, pan.s0[1] - (e.clientY - pan.y)/pan.k/H2);
});
const endPan = () => { pan = null; detpane.classList.remove("grabbing"); };
detpane.addEventListener("pointerup", endPan); detpane.addEventListener("pointercancel", endPan);

/* ---------- buttons ---------- */
$("s_angle").addEventListener("input", e => setAngle(e.target.value));
$("n_angle").addEventListener("change", e => setAngle(e.target.value));
$("r_m90").onclick = () => setAngle(A - 90);
$("r_p90").onclick = () => setAngle(A + 90);
$("r_0").onclick = () => setAngle(0);
$("c_reset").onclick = () => { C = inscribed(); drawCrop(); };
$("t_reset").onclick = () => setTone(DEF);
$("tif").onchange = updateCmd;
function setGuides(g){
  $("cropbox").dataset.g = g;
  document.querySelectorAll("#guides button").forEach(b => b.classList.toggle("on", b.dataset.g === g));
  try{ localStorage.setItem("guides", g); } catch(e){}
}
document.querySelectorAll("#guides button").forEach(b => b.onclick = () => setGuides(b.dataset.g));
let savedGuides = null; try{ savedGuides = localStorage.getItem("guides"); } catch(e){}
setGuides(["thirds","center","both","none"].includes(savedGuides) ? savedGuides : "thirds");
$("exp").addEventListener("click", async () => {
  $("exp").disabled = true; $("msg").textContent = "Exporting full resolution… (takes a few seconds)";
  try{
    const r = await fetch("/export?" + qs({crop: C.join(","), tif: $("tif").checked ? 1 : 0}), {method:"POST"});
    const j = await r.json();
    $("msg").textContent = j.error ? "Error: "+j.error : "Saved: " + j.files.join(", ");
  } catch(e){ $("msg").textContent = "Error: " + e; }
  $("exp").disabled = false;
});

/* ---------- opening files ---------- */
const WTEXT = $("wtext").textContent;
function showWelcome(title, text){
  $("welcome").classList.remove("loading");
  $("welcome").style.display = "flex"; $("wtitle").textContent = title; $("wtext").textContent = text || WTEXT;
}
function showLoading(step){
  showWelcome(step); $("welcome").classList.add("loading");
}
function applyInfo(info){
  DEF = info.defaults;
  if (info.app) $("cmd").style.display = "none";      // the packaged app has no command line to paste into
  if (!info.loaded){ showWelcome("Open a stacked image"); return; }
  loaded = true; window.FILE = info.file; W = info.W; H = info.H;
  showOriginal(info.n); setStep("Stretching and bringing out color…");
  $("file").textContent = ""; $("file").title = info.file;
  const sub = document.createElement("small"); sub.textContent = W + "×" + H + " after edge crop";
  $("file").append(info.name, sub);
  $("welcome").style.display = "none"; $("ctlwrap").classList.remove("off");
  $("msg").textContent = ""; cx = 0.5; cy = 0.5; detailSize();
  if (!built) buildControls();
  P = P.stretch ? P : {...DEF};
  setTone(P); setAngle(0);
}
async function openWith(url){
  $("open1").disabled = $("open2").disabled = true;
  showWelcome("Choose a file…", "A file picker window should have opened (it can hide behind the browser).");
  watchLoad();
  try{
    const r = await fetch(url, {method:"POST"}); const j = await r.json(); stopWatch();
    if (j.error) endIntro();
    if (j.error === "nodialog")
      showWelcome("Drag your image here", "No file picker is available on this computer. Drag your stacked TIFF onto this page instead.");
    else if (j.error) showWelcome("Couldn't open that file", j.error);
    else if (j.cancelled){ loaded ? $("welcome").style.display = "none" : showWelcome("Open a stacked image"); }
    else applyInfo(j);
  } catch(e){ stopWatch(); endIntro(); showWelcome("Error", String(e)); }
  $("open1").disabled = $("open2").disabled = false;
}

/* ---------- first look: show the stack as it really is, then sweep the result across it ---------- */
let intro = null, watcher = null;   // intro = {n, t0, revealing} while the original is on screen
function showOriginal(n){
  if (n == null || (intro && intro.n === n)) return;
  const u = "/original?n=" + n;
  intro = {n, t0: performance.now(), revealing: false};
  document.body.classList.add("intro");
  $("orig").style.clipPath = "none"; $("orig").src = u; $("prev").src = u; $("orig").style.display = "block";
  $("welcome").style.display = "none";
}
function setStep(t){ $("steps").textContent = t || ""; $("steps").style.display = t ? "block" : "none"; }
function endIntro(){
  intro = null; document.body.classList.remove("intro");
  $("orig").style.display = $("sweep").style.display = "none"; setStep("");
  drawMarker();
}
async function reveal(url){
  intro.revealing = true;
  const wait = 1200 - (performance.now() - intro.t0);         // give the original a moment on screen
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
  $("prev").src = url; try{ await $("prev").decode(); } catch(e){}
  setStep("");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return endIntro();
  const o = $("orig").style, s = $("sweep").style, T = 1700, t0 = performance.now();
  s.display = "block";
  await new Promise(done => requestAnimationFrame(function frame(now){
    const t = Math.min(1, Math.max(0, now - t0) / T), e = t < .5 ? 4*t*t*t : 1 - (2 - 2*t)**3 / 2;   // ease in-out
    o.clipPath = `inset(0 0 0 ${e*100}%)`; s.left = e*100 + "%";
    t < 1 ? requestAnimationFrame(frame) : done();
  }));
  endIntro();
}
function watchLoad(){             // poll while the server loads, to show the original and each step
  clearInterval(watcher);
  watcher = setInterval(async () => {
    try{
      const p = await (await fetch("/progress")).json();
      if (!p.busy) return;
      if (p.orig) showOriginal(p.n);
      if (intro) setStep(p.step); else showLoading(p.step || "Opening your image…");
    } catch(e){}
  }, 250);
}
function stopWatch(){ clearInterval(watcher); }
$("open1").onclick = $("open2").onclick = () => openWith("/open");

/* ---------- drag and drop: the browser only gives us the file's contents, so upload them ---------- */
let gone = false;
addEventListener("dragover", e => { e.preventDefault(); if (!gone) document.body.classList.add("dropping"); });
addEventListener("dragleave", e => { if (!e.relatedTarget) document.body.classList.remove("dropping"); });
addEventListener("drop", async e => {
  e.preventDefault(); document.body.classList.remove("dropping");
  const f = e.dataTransfer.files[0];
  if (!f || gone) return;
  $("open1").disabled = $("open2").disabled = true;
  showLoading("Opening " + f.name + "…");
  watchLoad();
  try{
    const r = await fetch("/upload?name=" + encodeURIComponent(f.name), {method:"POST", body:f}); const j = await r.json();
    stopWatch();
    if (j.error){ endIntro(); showWelcome("Couldn't open that file", j.error); } else applyInfo(j);
  } catch(e){ stopWatch(); endIntro(); showWelcome("Error", String(e)); }
  $("open1").disabled = $("open2").disabled = false;
});

/* ---------- lifetime: tell the server we're here, and when we leave ---------- */
function closed(){
  showWelcome("AstroBake has closed", "Start it again to keep working. You can close this tab.");
  gone = true; endIntro(); $("open1").style.display = "none";
  $("ctlwrap").classList.add("off"); $("quit").disabled = $("open2").disabled = true;
}
$("quit").onclick = async () => { try{ await fetch("/quit", {method:"POST"}); } catch(e){} closed(); };
setInterval(() => fetch("/ping").then(r => { if (!r.ok) throw 0; }).catch(closed), 20000);
addEventListener("pagehide", () => navigator.sendBeacon("/bye"));
(async () => { applyInfo(await (await fetch("/info")).json()); })();
</script></body></html>"""

def downloads_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def pick_file(initial_dir=""):
    """Native file dialog, run in a subprocess so it works from the server thread on every OS.
    Returns path, "" if cancelled, or None if no GUI toolkit is available."""
    me = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]
    fd, result = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    try:
        r = subprocess.run([*me, "--pick-file", initial_dir, result], capture_output=True, timeout=1800)
        if r.returncode != 0:
            return None
        with open(result, encoding="utf-8") as f: return f.read().strip()
    except Exception:
        return None
    finally:
        os.remove(result)


def run_ui(path=None, crop=True, port=8765, open_browser=True, out=None, auto_quit=False):
    """Serve the UI. With auto_quit (the packaged app has no console window to close) the server
    stops shortly after the last browser tab closes, or when nothing has been heard for a while."""
    S = {"path": None}                     # current image state
    lock = threading.Lock()
    DW, DH = 1100, 700                     # 1:1 detail window size (pixels)
    seen = {"last": time.monotonic(), "bye": None}
    prog = {"n": 0, "busy": False, "step": None, "orig": None}   # the load in progress, polled by the page

    def load_image(p, name=None):
        """Load a file from disk, or (with `name`) a temporary copy of a file dropped onto the page.
        A dropped file's real location is unknown to the browser, so its exports go to Downloads."""
        print(f"\nLoading {name or p}\nPreparing image (crop, gradient removal, white balance)…")
        prog.update(n=prog["n"] + 1, busy=True, step=None, orig=None)

        def report(step, img=None):
            prog["step"] = step
            if img is not None:   # the stack as it really looks, shown while the rest runs
                k = max(1, math.ceil(max(img.shape[:2]) / 1600))
                prog["orig"] = jpeg_bytes(img[::k, ::k])
        try:
            full, base = prepare(p, crop, report)
        finally:
            prog["busy"] = False
        H, W, _ = full.shape
        k = max(1, math.ceil(max(W, H) / 1600))
        S.clear(); gc.collect()
        if name:
            stem = os.path.splitext(name)[0]
            S.update(path=name, name=name, dir=None, out=os.path.join(downloads_dir(), stem + "_processed"))
        else:
            S.update(path=os.path.abspath(p), name=os.path.basename(p), dir=os.path.dirname(os.path.abspath(p)),
                     out=out if (out and p == path) else os.path.splitext(p)[0] + "_processed")
        S.update(full=full, base=base, W=W, H=H, k=k, n=prog["n"], orig=prog["orig"],
                 prev=np.ascontiguousarray(full[::k, ::k]))   # strided: keeps real per-pixel noise
        print("ready")

    def info():
        d = {"loaded": S.get("path") is not None, "defaults": DEFAULTS, "app": FROZEN, "n": S.get("n")}
        if d["loaded"]:
            d.update(file=S["path"], name=S["name"], W=S["W"], H=S["H"], DW=DW, DH=DH)
        return d

    def params(q):
        return {p: float(q.get(p, [DEFAULTS[p]])[0]) for p in PARAMS}

    def num(q, k, d=0.0):
        return float(q.get(k, [d])[0])

    def render_preview(s):
        st = dict(S["base"])
        return process(S["prev"].copy(), stats=st, px=S["k"], **s), st   # blur radii scale with preview size

    def render_detail(s, angle, cx, cy, dw=DW, dh=DH):
        """dw x dh window of the rotated full-res image at 1:1, centred on (cx, cy) of the rotated canvas."""
        W, H = S["W"], S["H"]
        M, W2, H2 = rot_geom(W, H, angle)
        dw, dh = min(dw, W2), min(dh, H2)
        x0 = int(np.clip(cx * W2 - dw / 2, 0, W2 - dw)); y0 = int(np.clip(cy * H2 - dh / 2, 0, H2 - dh))
        Minv = cv2.invertAffineTransform(M)
        pts = np.array([[x0, y0, 1], [x0 + dw, y0, 1], [x0, y0 + dh, 1], [x0 + dw, y0 + dh, 1]], float)
        src = pts @ Minv.T
        sx0, sy0 = (max(0, int(math.floor(v)) - 12) for v in src.min(0))
        sx1, sy1 = min(W, int(math.ceil(src[:, 0].max())) + 12), min(H, int(math.ceil(src[:, 1].max())) + 12)
        _, st = render_preview(s)                               # image-wide stats
        if sx1 <= sx0 or sy1 <= sy0:
            return np.zeros((dh, dw, 3), np.float32)
        sub = process(S["full"][sy0:sy1, sx0:sx1].copy(), stats=st, **s)
        M2 = M.copy(); M2[:, 2] += M[:, :2] @ np.array([sx0, sy0], float) - np.array([x0, y0], float)
        return cv2.warpAffine(sub, M2, (dw, dh), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def parse_request(self):
            ok = super().parse_request()
            if ok and self.path != "/bye":          # any request means a tab is still open
                seen["last"] = time.monotonic(); seen["bye"] = None
            return ok

        def send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def send_json(self, obj, code=200):
            self.send(json.dumps(obj).encode(), "application/json", code)

        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            try:
                if u.path == "/":
                    return self.send(UI_HTML.encode(), "text/html; charset=utf-8")
                if u.path == "/info":
                    return self.send_json(info())
                if u.path == "/ping":
                    return self.send(b"ok", "text/plain")
                if u.path == "/progress":
                    return self.send_json({k: prog[k] for k in ("n", "busy", "step")} | {"orig": prog["orig"] is not None})
                if u.path == "/original":
                    n = int(num(q, "n", -1))
                    img = prog["orig"] if n == prog["n"] else S.get("orig") if n == S.get("n") else None
                    return self.send(img, "image/jpeg") if img else self.send(b"gone", "text/plain", 404)
                if not S.get("path"):
                    return self.send(b"no image loaded", "text/plain", 409)
                if u.path == "/preview":
                    with lock:
                        img, _ = render_preview(params(q))
                        img = rotate(img, num(q, "angle"))
                    return self.send(jpeg_bytes(img), "image/jpeg")
                if u.path == "/detail":
                    with lock:
                        size = (int(np.clip(num(q, k, d), 64, 2400)) for k, d in (("dw", DW), ("dh", DH)))
                        img = render_detail(params(q), num(q, "angle"), num(q, "cx", .5), num(q, "cy", .5), *size)
                    return self.send(jpeg_bytes(img, 92), "image/jpeg")
                self.send(b"not found", "text/plain", 404)
            except Exception as e:
                self.send(str(e).encode(), "text/plain", 500)

        def do_POST(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            try:
                if u.path == "/bye":                    # a tab closed (sent by the page on pagehide)
                    seen["bye"] = time.monotonic()
                    return self.send(b"ok", "text/plain")
                if u.path == "/quit":
                    self.send(b"ok", "text/plain")
                    return threading.Thread(target=srv.shutdown).start()
                if u.path == "/open":
                    start = S.get("dir") or (os.path.expanduser("~") if FROZEN else os.getcwd())
                    p = pick_file(start)
                    if p is None: return self.send_json({"error": "nodialog"})
                    if p == "": return self.send_json({"cancelled": True})
                    if not os.path.isfile(p):
                        return self.send_json({"error": f"File not found: {p}"})
                    with lock: load_image(p)
                    return self.send_json(info())
                if u.path == "/upload":                 # file dropped onto the page
                    name = os.path.basename(q.get("name", ["image.tif"])[0])
                    left = int(self.headers.get("Content-Length", 0))
                    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(name)[1])
                    try:
                        with os.fdopen(fd, "wb") as f:
                            while left > 0:
                                chunk = self.rfile.read(min(left, 1 << 20))
                                if not chunk: break
                                f.write(chunk); left -= len(chunk)
                        with lock: load_image(tmp, name)
                    finally:
                        os.remove(tmp)                  # fully loaded into memory, the copy isn't needed
                    return self.send_json(info())
                if u.path == "/export":
                    if not S.get("path"): return self.send_json({"error": "no image loaded"})
                    s = params(q); tif = q.get("tif", ["1"])[0] == "1"
                    crop = [float(v) for v in q.get("crop", ["0,0,1,1"])[0].split(",")]
                    with lock:   # same code path as --batch, so UI export == command-line output
                        img = render_full(S["full"], S["base"], s, num(q, "angle"), crop)
                        files = save(img, S["out"], tif=tif); del img; gc.collect()
                    return self.send_json({"files": [os.path.abspath(f) for f in files]})
                self.send(b"not found", "text/plain", 404)
            except SystemExit:               # load() exits on unreadable TIFFs
                self.send_json({"error": "This TIFF needs an extra package: pip install imagecodecs"})
            except Exception as e:
                self.send_json({"error": str(e)})

    if path:
        load_image(path)

    srv = None
    for p in range(port, port + 20):         # find a free port if the default is taken
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler); break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"No free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"\nAstroBake running at {url}")
    print("Leave this window open while you use it. Close it, press Ctrl+C or click Quit in the page to quit.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    def watchdog():
        while True:
            time.sleep(1)
            now = time.monotonic()
            # 5 s grace lets a reload reconnect; 10 min covers browsers that throttle background tabs
            if (seen["bye"] and now - seen["bye"] > 5) or now - seen["last"] > 600:
                return srv.shutdown()
    if auto_quit:
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?", help="stacked TIFF (optional in UI mode)")
    p.add_argument("--batch", action="store_true", help="no UI: process straight to files")
    p.add_argument("--port", type=int, default=8765, help="web UI port (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    p.add_argument("-o", "--out", default=None, help="output name prefix")
    p.add_argument("--tif", action="store_true", help="batch: also write a 16-bit TIFF")
    p.add_argument("--no-crop", action="store_true", help="skip the automatic dead-edge crop")
    d = DEFAULTS
    p.add_argument("--stretch", type=float, default=d["stretch"], help="arcsinh strength: higher = brighter faint stuff, compressed core")
    p.add_argument("--target", type=float, default=d["target"], help="midtone brightness after stretch (0.05-0.35)")
    p.add_argument("--sky", type=float, default=d["sky"], help="final sky brightness (0-0.2)")
    p.add_argument("--scurve", type=float, default=d["scurve"], help="S-curve contrast strength (0-0.35)")
    p.add_argument("--sat", type=float, default=d["sat"], help="saturation multiplier")
    p.add_argument("--denoise", type=float, default=d["denoise"], help="background blur sigma, 0 = off")
    p.add_argument("--sharpen", type=float, default=d["sharpen"], help="sharpening of bright structure (0-2), 0 = off")
    p.add_argument("--temp", type=float, default=d["temp"], help="color temperature, -1 = cooler .. 1 = warmer")
    p.add_argument("--rotate", type=float, default=0.0, help="rotation in degrees, positive = clockwise")
    p.add_argument("--crop", default=None, help="x0,y0,x1,y1 as fractions of the rotated image "
                   "(default: largest crop with no empty corners)")
    args = p.parse_args()
    if args.input and not os.path.isfile(args.input):
        sys.exit(f"File not found: {args.input}")

    if not args.batch:
        return run_ui(args.input, not args.no_crop, args.port, not args.no_browser, args.out, auto_quit=FROZEN)

    if not args.input:
        sys.exit("--batch needs an input file")
    crop = [float(v) for v in args.crop.split(",")] if args.crop else None
    if crop is not None and len(crop) != 4:
        sys.exit("--crop needs four numbers: x0,y0,x1,y1")
    out = args.out or os.path.splitext(args.input)[0] + "_processed"
    settings = {k: getattr(args, k) for k in PARAMS}
    a, base_stats = prepare(args.input, not args.no_crop)
    print("settings:", settings, f"rotate={args.rotate}", f"crop={crop}")
    r = render_full(a, base_stats, settings, args.rotate, crop)
    save(r, out, tif=args.tif)


if __name__ == "__main__":
    main()
