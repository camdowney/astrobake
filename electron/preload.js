// The page's bridge to the desktop app (window.astrobake in astrobake.py's UI): native dialogs and the
// real path of a dropped file, so it is loaded in place instead of uploaded.
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("astrobake", {
  pickFile: start => ipcRenderer.invoke("pick-file", start),
  pickFolder: (kind, start) => ipcRenderer.invoke("pick-folder", kind, start),
  showFile: p => ipcRenderer.invoke("show-file", p),
  pathFor: file => { try { return webUtils.getPathForFile(file) || null; } catch (e) { return null; } },
});
