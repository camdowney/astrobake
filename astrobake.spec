# PyInstaller build of the desktop app's backend:  pyinstaller astrobake.spec
# Output: dist/astrobake-backend/ (a folder, so it starts fast), which electron-builder puts inside the app
# (see electron/package.json). The app runs it as  astrobake-backend --app  and talks to it over localhost.
a = Analysis(["astrobake.py"], excludes=["matplotlib", "IPython", "pytest", "tkinter"])
pyz = PYZ(a.pure)
# console=True keeps stdout working on Windows (the app reads the backend's address from it);
# the app starts it with no visible window
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="astrobake-backend", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="astrobake-backend", upx=False)
