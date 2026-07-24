import { app, BrowserWindow, dialog, ipcMain } from 'electron'
import { join } from 'node:path'
import { DaemonManager } from './daemon'
import { readSettings, writeSettings } from './settings'
import type { DaemonConfig } from '../shared/daemon-config'

// 全局单一 daemon：整个应用生命周期仅 spawn 一次。
const daemon = new DaemonManager()
let mainWindow: BrowserWindow | null = null

app.whenReady().then(boot).catch((err: unknown) => {
  dialog.showErrorBox('无法启动 Work Agent', String(err))
  app.quit()
})

async function boot(): Promise<void> {
  const config = await daemon.start()
  ipcMain.handle('daemon:config', () => daemon.getConfig())
  // 设置读写（M9.6）：仅在主进程访问 fs，渲染进程经 agentApi 调用。
  ipcMain.handle('settings:read', (_e, projectRoot: string) => readSettings(projectRoot))
  ipcMain.handle('settings:write', (_e, projectRoot: string, patch: Record<string, unknown>) =>
    writeSettings(projectRoot, patch),
  )
  createWindow(config)
}

function createWindow(config: DaemonConfig): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'Work Agent',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })
  mainWindow = window

  if (process.env.ELECTRON_RENDERER_URL) {
    void window.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void window.loadFile(join(__dirname, '../renderer/index.html'))
  }

  window.on('closed', () => {
    if (mainWindow === window) mainWindow = null
  })
}

app.on('activate', () => {
  if (mainWindow === null && BrowserWindow.getAllWindows().length === 0) {
    const config = daemon.getConfig()
    if (config) createWindow(config)
  }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  daemon.stop()
})
