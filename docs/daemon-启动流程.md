# Daemon 进程启动流程

## 一、主启动时序（flowchart）

```mermaid
flowchart TD
    A["CLI 入口<br/><code>python -m agent.cli daemon</code><br/>agent/cli.py"] --> B["加载全局 settings<br/>load_settings()<br/>仅用于网络配置 host/port/health_port/token"]
    B --> C["start_daemon(settings)<br/>agent/daemon/server.py"]

    C --> D["ensure_span_log_handler()<br/>初始化日志/span 处理器"]
    D --> E["定义惰性工厂闭包（多项目隔离）"]
    E --> E1["store_for(project_root)<br/>按项目惰性解析+缓存 SessionStore<br/>（首次才建 .agent/sessions/sessions.db）"]
    E --> E2["trace_store_for(project_root)<br/>按项目惰性解析+缓存 TraceStore<br/>（.agent/traces.db）"]
    E1 --> F
    E2 --> F

    F["定义会话工厂（新建/冷启动恢复双路径）"]
    F --> F1["session_factory(project_root, session_id)<br/>_build_session：已存在→Session.from_store 恢复<br/>否则→新建 Session"]
    F --> F2["restore_factory(project_root, session_id)<br/>session_id 在 store 中才重建，否则 None"]
    F1 --> G
    F2 --> G

    G["构造 SessionRegistry<br/>注入 session/transport/restore/store/trace 工厂"]
    G --> G1["registry._token = settings.daemon.token<br/>（可选 hello 鉴权）"]
    G1 --> H

    H["启动 HTTP /health 服务<br/>_start_health_server(host, health_port)<br/>独立端口，后台线程 serve_forever"]
    H --> I["asyncio.run( _serve(settings, registry, stop) )"]

    I --> I1["create_ws_server(registry, host, port)<br/>websockets.serve 绑定 host:port 开始监听"]
    I1 --> I2["进入监听后才宣告就绪<br/>打印 ws=... health=...（避免端口未就绪误判）"]
    I2 --> I3["后台预热 _prewarm()<br/>加载注册表 + 默认模型客户端（消冷启动延迟）"]
    I3 --> I4["await stop_event.wait()<br/>阻塞直到 Ctrl-C"]

    I4 --> J["收到 KeyboardInterrupt"]
    J --> K["收尾：httpd.shutdown() / server_close()<br/>进程退出"]
```

## 二、就绪后：客户端连接握手（flowchart）

```mermaid
flowchart LR
    P["前端/CLI 客户端<br/>WebSocket 连接 daemon"] --> P1["连接 ws://host:port"]
    P1 --> H1["_handler(ws, registry)<br/>创建 Connection 对象"]
    H1 --> H2["收到 HELLO<br/>携带 {client_type, version, token}"]
    H2 --> H3{"token 鉴权<br/>expected = registry._token"}

    H3 -- "token 不匹配" --> HE["回 ERROR {code:auth}<br/>结束连接"]
    H3 -- "token 匹配 / 未配置" --> HW["回 WELCOME<br/>{daemon_version, protocol_version}"]

    HW --> R["进入 _route 消息路由循环"]
    R --> R1["session.new → registry.new(project_root)<br/>建句柄+attach，回 SESSION_CREATED + ATTACHED"]
    R --> R2["session.attach → 冷启动恢复或取内存句柄，attach 连接"]
    R --> R3["session.switch → 顶替当前连接 attach 到目标会话"]
    R --> R4["session.list / task.send / show_tools / show_mcp …"]
```

## 关键设计点

1. **多项目隔离**：`settings` 只承载 daemon 的**网络配置**；每个会话按自己的 `project_root` 经 `load_settings(project_root=...)` 解析项目配置，`SessionStore`/`TraceStore` 路径锚定到项目根，互不串扰。
2. **惰性 + 缓存**：`store_for`/`trace_store_for` 首次调用才建库，同项目复用同一实例；daemon 启动不预建任何库。
3. **就绪判定**：WebSocket 服务真正开始监听后才打印就绪，避免 `waitForReady` 在端口未可连接时误判。
4. **后台预热**：`_prewarm` 提前加载注册表与默认模型客户端，消除首次 attach 的冷启动延迟；失败仅记日志，不阻断主流程。
5. **鉴权可选**：`settings.daemon.token` 为空则 hello 不鉴权；非空则 `token` 必须匹配，否则回 `{code:auth}`。
