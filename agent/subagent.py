"""子 Agent 生成器（M5.2）。

把子任务委派给一个**独立上下文窗口**的 ``AgentLoop`` 分身，主上下文只拿回文本摘要。
支持并行（asyncio.gather）、嵌套（深度限制 ``max_depth``）、模型降级、工具白名单、
独立沙箱/权限、fork（继承父 conv）、实时子任务渲染（``_SubAgentTransport``）。

设计要点（详见 milestones/M5-扩展能力/5.2-SubagentSpawner.md）：
- 子 agent 拥有**独立 EventStream 实例**，经 ``_SubAgentTransport`` 以「子任务视图」渲染，
  不混入父 EventStream；不弹出独立 HITL（澄清/审批由父代理统一决策）。
- Trace 父子：``loop.run(parent_span=)`` 显式挂载，配合独立 ``asyncio.Task`` 并行安全。
- fork：``share_history=True`` 时把父 conv 拷为子初始 messages（如 SessionMemory Compact
  的记忆子 agent 需读父对话历史）。
"""

from __future__ import annotations

import time

# ruff: noqa: E402  (agent.* 导入刻意置于 AgentSummary 定义之后，避免与 agent.core.loop 循环导入)
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.daemon.registry import SessionHandle, SessionRegistry

import yaml


@dataclass
class AgentSummary:
    """Subagent 类型的精简展示信息（供 CLI 渲染，不含 system_prompt 正文）。"""

    name: str
    description: str
    tools: list[str] | None
    disallowed_tools: list[str]
    model: str | None
    permission_mode: str | None
    builtin: bool
    source: str = "builtin"  # M11.6 来源：builtin / user / project


from agent.config.settings import Settings, user_config_path
from agent.context.compactors.session_memory import MEMORY_SYSTEM_PROMPT
from agent.context.tokens import _estimate_tokens
from agent.core.events import Event, EventStream, EventType
from agent.core.loop import AgentLoop, AgentResult
from agent.core.model import Message, Model, OpenAICompatibleModel
from agent.core.prompts import _split_frontmatter
from agent.core.transport import AgentTransport
from agent.obs.tracer import Tracer
from agent.runtime._subagent_tui_transport import _SubAgentTuiTransport
from agent.runtime.approval import ApprovalGate
from agent.runtime.registry import ToolRegistry, default_registry
from agent.runtime.sandbox import SandboxProfile, build_executor
from agent.runtime.terminal_transport import _SubAgentTransport
from agent.runtime.textual_transport import TextualTransport


# --------------------------------------------------------------------------- #
# AgentSpec：子 agent 的定义
# --------------------------------------------------------------------------- #
@dataclass
class AgentSpec:
    name: str
    description: str
    system_prompt: str  # 正文（替代默认 system 提示）
    tools: list[str] | None = None  # 白名单；None=继承所有
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None  # 覆盖 llm.model（None=inherit）
    permission_mode: str | None = None  # "plan"/"auto"/"dontAsk"/... 映射到 gate/sandbox
    max_turns: int | None = None
    effort: str | None = None
    isolation: str | None = None  # "worktree" 可选（M5 先留接口，不强制）
    share_history: bool = False  # True=fork 模式：继承父 conv（如记忆子 agent 需读父对话）
    no_control_tools: bool = False  # True=子 agent 不注入控制/虚拟工具（纯文本产出，强隔离）
    builtin: bool = False
    panel_height: int = 15  # 子 agent 输出框的固定行高（0=不限制）
    source: str = "builtin"  # M11.6 来源：builtin / user / project（用于面板分组展示）
    source_dir: Path | None = None  # 定义文件所在目录（内置为 None；供编辑/写回定位）


# --------------------------------------------------------------------------- #
# 内置类型常量
# --------------------------------------------------------------------------- #
BUILTIN_EXPLORE = AgentSpec(
    name="explore",
    description="快速代码库搜索（只读，跳过会话文件）",
    system_prompt=(
        "你是代码探索专家，只做只读搜索（read/grep/glob/bash）。\n"
        "绝不修改任何文件，不调用 write/edit。聚焦于定位代码、理解结构、"
        "汇总发现，并以简洁文本返回结果。"
    ),
    tools=["read", "grep", "glob", "bash"],
    disallowed_tools=["write", "edit"],
    permission_mode="plan",
    builtin=True,
)

BUILTIN_PLAN = AgentSpec(
    name="plan",
    description="plan mode 期间研究（只读）",
    system_prompt=(
        "你是研究规划专家，只做只读搜索（read/grep/glob/bash），不修改文件。\n"
        "基于探索结果产出清晰、可执行的计划（步骤、风险、依赖），以文本返回。"
    ),
    tools=["read", "grep", "glob", "bash"],
    disallowed_tools=["write", "edit"],
    permission_mode="plan",
    builtin=True,
)

BUILTIN_GENERAL = AgentSpec(
    name="general-purpose",
    description="复杂多步骤（探索+修改）",
    system_prompt=(
        "你是通用执行 agent，可探索与修改代码。\n"
        "先理解任务与上下文，再按需调用工具推进；完成后以简洁文本总结成果。"
    ),
    tools=None,
    builtin=True,
)

# M4.4 记忆子 agent：复用 M5.4.1 后台 Subagent 机制，在后台增量维护会话摘要。
# 强隔离：tools=[] 且无控制工具（no_control_tools），只从 fork 的对话历史产出 10 段
# markdown 摘要文本；结果由父 Session 落盘到 summary.md（绝不触碰项目代码）。
BUILTIN_SESSION_MEMORY = AgentSpec(
    name="session-memory",
    description="后台增量维护会话摘要（记忆子 agent，纯文本产出）",
    system_prompt=MEMORY_SYSTEM_PROMPT,
    tools=[],
    no_control_tools=True,
    share_history=True,
    builtin=True,
)

BUILTIN_SPECS: tuple[AgentSpec, ...] = (
    BUILTIN_EXPLORE,
    BUILTIN_PLAN,
    BUILTIN_GENERAL,
    BUILTIN_SESSION_MEMORY,
)


# --------------------------------------------------------------------------- #
# SubagentSpawner
# --------------------------------------------------------------------------- #
class SubagentSpawner:
    def __init__(
        self,
        settings: Settings,
        *,
        tracer: Tracer | None = None,
        max_depth: int = 5,
        cwd: Path | None = None,
    ) -> None:
        self.settings = settings
        self.tracer = tracer
        self.max_depth = max_depth
        self.cwd = Path(cwd) if cwd else Path.cwd()

    # ------------------------------------------------------------------ #
    # 发现 / 获取
    # ------------------------------------------------------------------ #
    def discover(self) -> list[AgentSpec]:
        """扫描 <project>/.agent/agents/*.md 与 ~/.agent/agents/*.md（项目级覆盖同名）。

        内置类型始终可用；用户级可覆盖内置同名，项目级再覆盖用户级。
        项目级目录用 spawner 自身的 project_root（self.cwd）定位，而非环境变量/进程 cwd，
        以保证多项目 daemon 下各自扫到本项目的 agent（与 settings 加载约定一致）。
        """
        specs: dict[str, AgentSpec] = {b.name: b for b in BUILTIN_SPECS}

        user_dir = user_config_path().parent / "agents"
        project_dir = self.cwd / ".agent" / "agents"
        for d, source in ((user_dir, "user"), (project_dir, "project")):  # 后写覆盖先写
            if d.is_dir():
                for f in sorted(d.glob("*.md")):
                    spec = self._parse_agent_file(f)
                    if spec is not None:
                        spec.source = source
                        spec.source_dir = d
                        specs[spec.name] = spec
        return list(specs.values())

    def get(self, name: str) -> AgentSpec | None:
        for s in self.discover():
            if s.name == name:
                return s
        return None

    def catalog_prompt(self) -> str:
        """触发目录（name + description），供注入系统提示（类比 skills_catalog）。

        只暴露「有哪些 agent、何时用」，绝不把 agent 的 system_prompt 正文灌进系统提示。
        内置类型始终列出；用户级/项目级自定义 agent 同名覆盖后也一并列出。
        """
        lines = []
        for s in self.discover():
            _scope = "（内置）" if s.builtin else "（自定义）"
            _tools = ""
            if s.tools is not None:
                _tools = f" 工具白名单: {', '.join(s.tools)}"
            elif s.disallowed_tools:
                _tools = f" 禁用: {', '.join(s.disallowed_tools)}"
            lines.append(f"- {s.name}{_scope}: {s.description}{_tools}")
        return "\n".join(lines)

    def summaries(self) -> list[AgentSummary]:
        """M5.4：返回精简列表（name + 描述 + tools + model + 权限），不含 system_prompt 正文。

        每次调用重新 ``discover()``（实时检测会话中新加的 agent 定义文件）。
        """
        return [
            AgentSummary(
                name=s.name,
                description=s.description,
                tools=list(s.tools) if s.tools is not None else None,
                disallowed_tools=list(s.disallowed_tools),
                model=s.model,
                permission_mode=s.permission_mode,
                builtin=s.builtin,
                source=s.source,
            )
            for s in self.discover()
        ]

    def _parse_agent_file(self, path: Path) -> AgentSpec | None:
        """解析一个 agent 定义 .md（YAML frontmatter + Markdown 正文）。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta_raw, body = _split_frontmatter(text)
        meta: dict[str, Any] = yaml.safe_load(meta_raw) if meta_raw else {}
        if not isinstance(meta, dict):
            meta = {}
        name = str(meta.get("name") or path.stem)
        system_prompt = str(meta.get("system_prompt") or body).strip()
        if not system_prompt:
            return None
        tools = meta.get("tools")
        if tools is not None and not isinstance(tools, list):
            tools = [str(t) for t in tools]
        return AgentSpec(
            name=name,
            description=str(meta.get("description", "")),
            system_prompt=system_prompt,
            tools=tools,  # type: ignore[arg-type]
            disallowed_tools=list(meta.get("disallowed_tools") or []),
            model=meta.get("model"),
            permission_mode=meta.get("permission_mode"),
            max_turns=meta.get("max_turns"),
            effort=meta.get("effort"),
            isolation=meta.get("isolation"),
            share_history=bool(meta.get("share_history", False)),
            builtin=False,
            panel_height=int(meta.get("panel_height", 15)),
        )

    def update_spec(self, name: str, updates: dict[str, Any]) -> bool:
        """M11.6 编辑智能体：把 updates 合并进该 agent 的 .md frontmatter 并写回。

        仅支持非内置（用户/项目级）agent；内置 agent 是代码定义，不可编辑。
        找到定义文件（source_dir/name.md）失败返回 False。
        """
        spec = self.get(name)
        if spec is None or spec.builtin or spec.source_dir is None:
            return False
        path = spec.source_dir / f"{name}.md"
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
            meta_raw, body = _split_frontmatter(text)
            meta: dict[str, Any] = yaml.safe_load(meta_raw) if meta_raw else {}
            if not isinstance(meta, dict):
                meta = {}
            for k, v in updates.items():
                if k == "system_prompt":
                    # 正文单独写；frontmatter 不存 system_prompt（解析时 fallback body）
                    if v is not None:
                        body = str(v)
                    continue
                if v is None:
                    meta.pop(k, None)
                else:
                    meta[k] = v
            # 合并后 body 已按需更新；重新序列化 frontmatter
            new_frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
            path.write_text(f"---\n{new_frontmatter}\n---\n{body}", encoding="utf-8")
            self._cache = None  # type: ignore[attr-defined]  # 清空 discover 缓存（若有）
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    # 生成
    # ------------------------------------------------------------------ #
    async def spawn(
        self,
        spec: AgentSpec,
        task: str,
        *,
        depth: int = 0,
        parent_span=None,
        base_registry: ToolRegistry | None = None,
        base_model: Model | None = None,
        parent_transport: AgentTransport | None = None,
        parent_messages: list[Message] | None = None,
        parent_sandbox: Any | None = None,
        parent_gate: ApprovalGate | None = None,
        live: bool = True,
        # M9 subsession：daemon 模式下给定父 handle + registry 时，子 agent 走独立
        # subsession（事件经父连接多路复用、带 subsession_id，桌面端实时渲染）。
        # CLI 模式不传，保持现有本地 transport，行为不变（向后兼容）。
        parent_handle: SessionHandle | None = None,
        registry: SessionRegistry | None = None,
        # M10.2：子 agent USAGE 事件的父 message 指针（指向派生子 agent 的那条 message）；
        # 由调用方（loop._tool_spawn_subagent）传入当前 message_id，逐级形成 message 树。
        parent_message_id: str | None = None,
        # M11：后台 subsession 标记（如 session-memory 记忆子 agent）。后台 subsession 事件
        # 仍走 subsession 实时转发（供持久化/后台面板），但带 background=true，
        # 前端据此不渲染成前台聊天区的子 agent 卡。
        background: bool = False,
    ) -> AgentResult:
        """构造独立 AgentLoop（独立 EventStream + fork 可选），跑 run()，返回摘要。"""
        if depth >= self.max_depth:
            raise RecursionError(f"subagent depth limit {self.max_depth} reached")

        import uuid as _uuid

        sub_message_id = _uuid.uuid4().hex  # M10.2：子 message 自有 message_id（供 USAGE 事件打标）

        # ① 工具白名单：base_registry 子集
        sub_reg = self._subset_registry(base_registry or default_registry, spec)
        # ② 模型降级：spec.model 覆盖
        sub_model = self._resolve_model(base_model, spec)
        # ③ 沙箱/权限：permission_mode 映射（plan→read-only + 跳过 exec gate）
        sub_sandbox, sub_gate = self._resolve_security(spec, parent_sandbox, parent_gate)
        # ④ fork：share_history=True 时继承父 conv
        initial = list(parent_messages) if (spec.share_history and parent_messages) else []

        # daemon 模式 + 给定父 handle/registry：走独立 subsession 实时转发（桌面端可见）。
        if parent_handle is not None and registry is not None:
            from agent.core.events import EventStream
            from agent.daemon.bridge import SubsessionBridgeTransport
            from agent.daemon.registry import SessionHandle as _SubHandle

            sub_id = f"{parent_handle.session_id}/sub_{spec.name}_{depth}_{_uuid.uuid4().hex[:6]}"
            sub_handle = _SubHandle(
                sub_id,
                spec.name,
                None,
                None,
                parent_handle.project_root,
                parent_id=parent_handle.session_id,
                background=background,
            )
            registry.register_subsession(parent_handle.session_id, sub_handle)
            sub_stream = EventStream()
            # M11：后台 subsession（如 session-memory）事件源头统一打 background 标记，
            # 使实时转发与 sqlite 落盘都保留，回放时前端据此不渲染进前台聊天区。
            if background:
                sub_stream.background = True
            sub_transport = SubsessionBridgeTransport(parent_handle, sub_handle)
            # M9 subsession 持久化：把子会话事件带 parent_session_id 落盘，供重进后回放
            # 历史（修复「子 agent 历史丢失 / 后台列表完成即消失」）。仓库按项目隔离，
            # 经 registry._store_factory 取得；无 store（测试/CLI 兼容）时跳过。
            store_factory = getattr(registry, "_store_factory", None)
            if store_factory is not None:
                try:
                    from agent.context.session_store import SessionStoreSink

                    _store = store_factory(parent_handle.project_root)
                    sub_stream.subscribe(
                        SessionStoreSink(
                            _store,
                            sub_id,
                            parent_session_id=parent_handle.session_id,
                        )
                    )
                except Exception:
                    # 持久化失败不应阻断子 agent 执行
                    pass
        else:
            # 父传输为 Textual TUI 时，子 agent 渲染走 _SubAgentTuiTransport（前缀汇入主区）；
            # 否则沿用旧 _SubAgentTransport（rich 面板集）。两者行为对齐、互不污染。
            if isinstance(parent_transport, TextualTransport):
                sub_transport = _SubAgentTuiTransport(
                    parent=parent_transport,
                    name=spec.name,
                )
            else:
                sub_transport = _SubAgentTransport(
                    parent=parent_transport,
                    name=spec.name,
                    panel_height=spec.panel_height,
                    live=live,
                )
            sub_stream = None  # 默认 loop.run 内部新建独立 stream

        # max_turns 限制：克隆 settings 覆盖循环上限
        sub_settings = self.settings
        if spec.max_turns is not None:
            sub_settings = self.settings.model_copy(deep=True)
            sub_settings.loop.max_iterations = spec.max_turns

        loop = AgentLoop(
            sub_model,
            sub_reg,
            sub_settings,
            tracer=self.tracer,
            sandbox=sub_sandbox,
            gate=sub_gate,
            cwd=self.cwd,
        )
        # 强隔离：记忆子 agent 等场景禁止控制/虚拟工具（只产出文本，不能委派/加载 skill）
        loop._control_tools_enabled = not spec.no_control_tools
        # 让子 loop 继承当前深度，使嵌套 spawn 能正确累加（depth+1 传入）
        loop._current_depth = depth
        t0 = time.time()  # M10.2：子 agent 独立计 duration
        try:
            result = await loop.run(
                task,
                messages=initial,
                transport=sub_transport,
                stream=sub_stream,
                system_prompt=spec.system_prompt or None,
                parent_span=parent_span,
                name=spec.name,
                message_id=sub_message_id,  # M10.2：子 message 自有 message_id
            )
        finally:
            # loop.run 不会关闭 transport；必须显式关闭，否则子 agent 面板 slot 不注销、
            # hub 的 Live 残留继续占用终端，污染后续父 agent 输出。
            sub_transport.close()
        # M10.2：daemon 子 agent 用量作为 USAGE 事件落盘（parent_message_id 形成 message 树，
        # 前端据此归集到派生子 agent 的那条 message）。CLI 分支 sub_stream=None 跳过。
        if sub_stream is not None and parent_handle is not None:
            self._emit_subagent_usage(
                sub_stream, result, time.time() - t0, parent_message_id=parent_message_id
            )
        return result

    # ------------------------------------------------------------------ #
    # M10.2：子 agent USAGE 事件辅助
    # ------------------------------------------------------------------ #
    def _emit_subagent_usage(
        self,
        stream: EventStream,
        result: AgentResult,
        duration: float,
        *,
        parent_message_id: str | None,
    ) -> None:
        """把子 agent 一次响应的 token 用量作为 USAGE 事件落盘（带 parent_message_id）。

        usage 为空时退化为估算 token 数，标记 estimated=True（与历史 report_usage 一致）。
        """
        mid = result.message_id or stream.current_message_id
        if mid is None:
            return
        usage = result.usage
        if not usage:
            est = _estimate_tokens(result.text or "")
            stream.append(
                Event(
                    type=EventType.USAGE,
                    message_id=mid,
                    parent_message_id=parent_message_id,
                    usage={"estimated_tokens": est},
                    duration=duration,
                    estimated=True,
                )
            )
            return
        stream.append(
            Event(
                type=EventType.USAGE,
                message_id=mid,
                parent_message_id=parent_message_id,
                usage=dict(usage),
                duration=duration,
                estimated=False,
            )
        )

    # ------------------------------------------------------------------ #
    # 内部解析
    # ------------------------------------------------------------------ #
    def _subset_registry(self, base: ToolRegistry, spec: AgentSpec) -> ToolRegistry:
        specs = base.list()
        if spec.disallowed_tools:
            drop = set(spec.disallowed_tools)
            specs = [s for s in specs if s.name not in drop]
        if spec.tools is not None:
            allowed = set(spec.tools)
            specs = [s for s in specs if s.name in allowed]
        reg = ToolRegistry()
        for s in specs:
            reg.register(s)
        return reg

    def _resolve_model(self, base_model: Model | None, spec: AgentSpec) -> Model:
        if spec.model:
            # 同 settings 但换模型名；复用 base 的 api_key/base_url
            return OpenAICompatibleModel(
                api_key=self.settings.llm.api_key,
                base_url=self.settings.llm.base_url,
                model=spec.model,
            )
        return base_model  # type: ignore[return-value]

    def _resolve_security(
        self, spec: AgentSpec, parent_sandbox: Any | None, parent_gate: ApprovalGate | None
    ) -> tuple[Any, ApprovalGate]:
        if spec.permission_mode == "plan":
            sandbox = build_executor("local", workspace=self.cwd, profile=SandboxProfile.READ_ONLY)
            gate = ApprovalGate("never")
            return sandbox, gate
        # 默认：继承父 sandbox/gate；无父则用 settings 默认
        if parent_sandbox is not None:
            return parent_sandbox, parent_gate  # type: ignore[return-value]
        sandbox = build_executor(
            "local",
            workspace=self.cwd,
            profile=SandboxProfile(self.settings.sandbox.profile),
        )
        gate = ApprovalGate(self.settings.approval.mode)
        return sandbox, gate
