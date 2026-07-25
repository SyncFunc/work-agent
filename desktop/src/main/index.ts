import { app, BrowserWindow, Menu, dialog, ipcMain } from 'electron'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { DaemonManager } from './daemon'
import { readSettings, writeSettings } from './settings'

// 全局单一 daemon：整个应用生命周期仅 spawn 一次。
const daemon = new DaemonManager()
let mainWindow: BrowserWindow | null = null

app.whenReady().then(boot).catch((err: unknown) => {
  dialog.showErrorBox('无法启动 Work Agent', String(err))
  app.quit()
})

async function boot(): Promise<void> {
  await daemon.start()
  ipcMain.handle('daemon:config', () => daemon.getConfig())
  // 设置读写（M9.6）：仅在主进程访问 fs，渲染进程经 agentApi 调用。
  ipcMain.handle('settings:read', (_e, projectRoot: string) => readSettings(projectRoot))
  ipcMain.handle('settings:write', (_e, projectRoot: string, patch: Record<string, unknown>) =>
    writeSettings(projectRoot, patch),
  )
  // M9.9：顶栏自绘菜单接管，移除原生菜单（避免与自绘菜单重复）。
  Menu.setApplicationMenu(null)
  registerWindowHandlers()
  createWindow()
}

// M9.9 窗口控制 + 打开文件夹：供自绘顶栏调用（frameless 窗口必须自带控制按钮）。
function registerWindowHandlers(): void {
  const focused = (): BrowserWindow | null =>
    mainWindow ?? BrowserWindow.getFocusedWindow()
  ipcMain.handle('window:minimize', () => focused()?.minimize())
  ipcMain.handle('window:toggleMaximize', () => {
    const w = focused()
    if (!w) return
    if (w.isMaximized()) w.unmaximize()
    else w.maximize()
  })
  ipcMain.handle('window:close', () => focused()?.close())
  ipcMain.handle('window:reload', () => focused()?.reload())
  ipcMain.handle('app:quit', () => app.quit())
  // 打开文件夹（切换工作区）：原生目录选择 → 经 project:open 推送渲染进程。
  ipcMain.handle('window:openFolder', () => {
    const win = focused()
    if (!win) return
    void dialog
      .showOpenDialog(win, { properties: ['openDirectory'], title: '选择项目目录' })
      .then((res) => {
        if (!res.canceled && res.filePaths.length > 0) {
          win.webContents.send('project:open', res.filePaths[0])
        }
      })
  })
}

// preload 产物扩展名随构建格式变化（CJS→.cjs / ESM→.mjs / 旧 ESM→.js），
// 这里依次尝试，避免 dev/build 下因文件名对不上导致 preload 加载失败。
function resolvePreload(): string {
  for (const name of ['index.cjs', 'index.mjs', 'index.js']) {
    const p = join(__dirname, '..', 'preload', name)
    if (existsSync(p)) return p
  }
  return join(__dirname, '../preload/index.cjs')
}

function createWindow(): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    // M9.9：frameless 自绘顶栏（去掉系统标题栏与边框）。thickFrame 在 Windows 下保留可拖拽边缘缩放。
    frame: false,
    thickFrame: true,
    title: 'Work Agent',
    webPreferences: {
      preload: resolvePreload(),
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
    if (config) createWindow()
  }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  daemon.stop()
})
