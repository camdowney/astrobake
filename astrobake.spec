# PyInstaller build for the standalone app:  pyinstaller astrobake.spec
# Windows/Linux: one self-contained executable in dist/.  macOS: dist/Astrobake.app.
import sys

a = Analysis(["astrobake.py"], excludes=["matplotlib", "IPython", "pytest"])
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Astrobake", console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="Astrobake")
    app = BUNDLE(coll, name="Astrobake.app",
                 # no windows of its own (the UI is in the browser), so stay out of the Dock
                 info_plist={"LSUIElement": True, "NSHighResolutionCapable": True})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Astrobake", console=False, upx=False)
