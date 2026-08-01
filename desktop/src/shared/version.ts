// 桌面端版本唯一来源：package.json。
// 所有运行时代码（App.tsx 开屏动画 / Sidebar 等）统一从这里取，避免多份硬编码漂移。
// 注意：src/renderer/index.html 里的静态启动遮罩在 JS 挂载前渲染，无法 import 模块，
// 那里仍为手动填写，需与 package.json 的 version 保持一致（发版时同步改）。
import pkg from '../../package.json'

export const APP_NAME = 'Work Agent'
export const APP_VERSION: string = pkg.version
