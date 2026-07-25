// 项目根持久化（localStorage）。渲染进程无 node 访问，用 localStorage 存储当前项目根；
// 主进程菜单「打开项目…」选目录后经 IPC 推送，渲染进程调用 saveProjectRoot 持久化。

const STORAGE_KEY = 'workagent.projectRoot'

/** 持久化项目根到 localStorage（菜单「打开项目…」或首次打开时调用）。 */
export function saveProjectRoot(root: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, root)
  } catch {
    /* localStorage 不可用时忽略 */
  }
}

/** 读取持久化的项目根（无则回退默认）。 */
export function loadProjectRoot(fallback: string): string {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v && v.length > 0) return v
  } catch {
    /* ignore */
  }
  return fallback
}
