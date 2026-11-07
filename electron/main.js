// Astrobake desktop app: starts the Python backend (astrobake.py --app) and shows its page in a native
// window. The backend does all the work; this file only owns the window, dialogs and the backend's lifetime.
const { app, BrowserWindow, dialog, ipcMain, shell, Menu } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

let win = null, backend = null, url = null, quitting = false;
const log = [];                                    // backend output, shown if it fails to start

function backendCommand(extra) {
  if (app.isPackaged) {                            // PyInstaller build placed in resources/ by electron-builder
    const exe = path.join(process.resourcesPath, "backend", process.platform === "win32" ? "astrobake-backend.exe" : "astrobake-backend");
    return [exe, ["--app", "--port", "0", ...extra]];
  }
  const root = path.join(__dirname, "..");         // development: the script next to this folder
  const venv = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const py = process.env.ASTROBAKE_PYTHON || (fs.existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3");
  return [py, ["-u", path.join(root, "astrobake.py"), "--app", "--port", "0", ...extra]];
}

function startBackend() {
  // a file passed on the command line (or "Open with") is loaded at start, as with astrobake.py <file>
  const file = process.argv.slice(app.isPackaged ? 1 : 2).find(a => !a.startsWith("-") && fs.existsSync(a));
  const [cmd, args] = backendCommand(file ? [path.resolve(file)] : []);
  return new Promise((resolve, reject) => {
    backend = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
                                 env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
    let buf = "";
    const onData = chunk => {
      const text = chunk.toString();
      process.stdout.write(text);
      log.push(text); if (log.length > 200) log.shift();
      buf += text;
      const m = buf.match(/ASTROBAKE_URL (\S+)/);
      if (m && !url) { url = m[1]; resolve(url); }
    };
    backend.stdout.on("data", onData);
    backend.stderr.on("data", onData);
    backend.on("error", err => reject(new Error(`Couldn't start ${cmd}: ${err.message}`)));
    backend.on("exit", code => {
      backend = null;
      if (!url) reject(new Error(`The Astrobake engine stopped (exit code ${code}).`));
      else if (!quitting) {
        dialog.showErrorBox("Astrobake stopped", "The Astrobake engine stopped unexpectedly.\n\n" + log.join("").slice(-2000));
        app.quit();
      }
    });
  });
}

function stopBackend() {
  if (!backend) return;
  try { backend.stdin.end(); } catch (e) {}        // the backend quits when its stdin closes
  const b = backend;
  setTimeout(() => { try { b.kill(); } catch (e) {} }, 3000);
}

async function stacking() {
  try {
    const s = await (await fetch(url + "stackprogress")).json();
    return s.state === "stacking" || s.state === "loading";
  } catch (e) { return false; }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 900, minHeight: 600, backgroundColor: "#111111", show: false,
    title: "Astrobake", icon: path.join(__dirname, "build", "icon.png"),
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true },
  });
  win.once("ready-to-show", () => win.show());
  win.loadFile(path.join(__dirname, "starting.html"));

  // only the backend's page lives in the window; links elsewhere open in the browser
  const ours = u => url && u.startsWith(url);
  win.webContents.setWindowOpenHandler(({ url: u }) => { if (/^https?:/.test(u)) shell.openExternal(u); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, u) => { if (!ours(u)) { e.preventDefault(); if (/^https?:/.test(u)) shell.openExternal(u); } });

  win.on("close", async e => {
    if (quitting || !url) return;
    e.preventDefault();
    if (await stacking()) {
      const r = await dialog.showMessageBox(win, {
        type: "warning", buttons: ["Keep stacking", "Quit"], defaultId: 0, cancelId: 0,
        message: "A stack is still being made.", detail: "If you quit now it stops and nothing is saved.",
      });
      if (r.response === 0) return;
    }
    quitting = true; win.close();
  });
  win.on("closed", () => { win = null; app.quit(); });
}

ipcMain.handle("pick-file", async (e, start) => {
  const r = await dialog.showOpenDialog(win, {
    title: "Open stacked image", defaultPath: start || undefined, properties: ["openFile"],
    filters: [{ name: "TIFF or FITS images", extensions: ["tif", "tiff", "fit", "fits", "fts"] }, { name: "All files", extensions: ["*"] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

const FOLDER_TITLES = { lights: "Choose the folder of light frames", dark: "Choose the dark frames folder",
                        flat: "Choose the flat frames folder", bias: "Choose the bias frames folder" };
ipcMain.handle("pick-folder", async (e, kind, start) => {
  const r = await dialog.showOpenDialog(win, {
    title: FOLDER_TITLES[kind] || "Choose a folder", defaultPath: start || undefined, properties: ["openDirectory"],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("show-file", (e, p) => { if (typeof p === "string" && fs.existsSync(p)) shell.showItemInFolder(p); });

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });

  app.whenReady().then(async () => {
    if (process.platform !== "darwin") Menu.setApplicationMenu(null);   // macOS keeps its standard app menu
    createWindow();
    try {
      await startBackend();
      if (win) win.loadURL(url);
    } catch (err) {
      quitting = true;
      dialog.showErrorBox("Astrobake couldn't start", err.message + "\n\n" + log.join("").slice(-2000));
      app.quit();
    }
  });

  app.on("window-all-closed", () => app.quit());   // one window; closing it quits on macOS too
  app.on("before-quit", e => {                     // Cmd+Q / taskbar Quit: ask the window first, as closing it does
    if (!quitting && win && url) { e.preventDefault(); win.close(); }
  });
  app.on("will-quit", stopBackend);
}
