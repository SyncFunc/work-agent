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
  createMenu()
  createWindow()
}

// 自定义应用菜单：去掉 Electron 默认全套（File/Edit/View/Window/Help），仅保留所需项。
// 「打开项目…」用原生目录选择对话框选目录，经 IPC（project:open）推送给渲染进程。
function createMenu(): void {
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: '文件',
      submenu: [
        {
          label: '打开项目…',
          click: () => {
            const win = mainWindow ?? BrowserWindow.getFocusedWindow()
            if (!win) return
            void dialog
              .showOpenDialog(win, { properties: ['openDirectory'], title: '选择项目目录' })
              .then((res) => {
                if (!res.canceled && res.filePaths.length > 0) {
                  win.webContents.send('project:open', res.filePaths[0])
                }
              })
          },
        },
        { type: 'separator' },
        { label: '退出', role: 'quit' },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
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
