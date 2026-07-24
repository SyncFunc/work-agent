# Work Agent 桌面客户端（Electron + React + TypeScript）

基于 `agentrunner` daemon（`python -m agent.cli daemon`）的桌面客户端。Electron 主进程在
应用启动时一次性 spawn 全局单一 daemon，轮询 `/health` 直到就绪，再把 WebSocket 地址 +
token 经 `contextBridge` 注入渲染进程（React 应用）。

> 开发期依赖系统已安装的 Python 3.12+，桌面端不打包 Python（打包分发后续再定）。

## 前置

```bash
# 仓库根目录：安装 agent 包（daemon 代码来自本仓库）
pip install -e ".[dev]"

# desktop 目录：安装前端依赖
cd desktop
npm install
```

可选：用 `AGENT_PYTHON` 指定 Python 解释器，例如：

```bash
AGENT_PYTHON=/usr/bin/python3.12 npm run dev
```

## 开发

```bash
npm run dev      # 启动 Electron（含 Vite 热重载），主进程自动拉起 daemon
```

窗口出现前，主进程已 `spawn` daemon 并确认 `/health` 返回 200。

## 校验 / 构建

```bash
npm run lint     # tsc --noEmit 类型检查（CI 等价门禁）
npm run build    # electron-vite 构建 main/preload/renderer 到 out/
npm run preview  # 预览构建产物
```

## 目录结构

```
desktop/
├── electron.vite.config.ts   # 三目标构建（main / preload / renderer）
├── tsconfig.json
├── src/
│   ├── main/
│   │   ├── index.ts          # 主进程入口：app 生命周期 + 创建窗口
│   │   ├── daemon.ts         # daemon 生命周期（spawn / health 轮询 / kill）
│   │   └── python.ts         # 定位 Python 解释器（AGENT_PYTHON → python → python3）
│   ├── preload/
│   │   └── index.ts          # contextBridge 暴露只读 daemon 配置
│   ├── renderer/             # React 应用（M9.2+ 填充 UI）
│   └── shared/
│       └── daemon-config.ts  # 主/渲染共享的 DaemonConfig 类型
```

## 不变量

- 整个应用生命周期内 daemon 进程数 = 1。
- 项目根切换（M9.3）**不**触发 daemon 重启。
- token 仅经 `contextBridge` 注入渲染进程，不出现在窗口地址栏，渲染进程无任何命令执行能力。
