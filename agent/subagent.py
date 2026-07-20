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

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.live import Live
from rich.panel import Panel


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

from agent.config.settings import Settings, project_config_path, user_config_path
from agent.core.loop import AgentLoop, AgentResult
from agent.core.model import Message, Model, OpenAICompatibleModel
from agent.core.prompts import _split_frontmatter
from agent.core.transport import AgentTransport
from agent.obs.tracer import Tracer
from agent.runtime.approval import ApprovalGate
from agent.runtime.registry import ToolRegistry, default_registry
from agent.runtime.sandbox import SandboxProfile, build_executor
from agent.runtime.terminal_transport import TerminalTransport


# --------------------------------------------------------------------------- #
# AgentSpec：子 agent 的定义
# --------------------------------------------------------------------------- #
@dataclass
class AgentSpec:
    name: str
    description: str
    system_prompt: str                 # 正文（替代默认 system 提示）
    tools: list[str] | None = None     # 白名单；None=继承所有
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None           # 覆盖 llm.model（None=inherit）
    permission_mode: str | None = None  # "plan"/"auto"/"dontAsk"/... 映射到 gate/sandbox
    max_turns: int | None = None
    effort: str | None = None
    isolation: str | None = None       # "worktree" 可选（M5 先留接口，不强制）
    share_history: bool = False        # True=fork 模式：继承父 conv（如记忆子 agent 需读父对话）
    builtin: bool = False
    panel_height: int = 15             # 子 agent 输出框的固定行高（0=不限制）


# --------------------------------------------------------------------------- #
# 内置类型常量
# --------------------------------------------------------------------------- #
BUILTIN_EXPLORE = AgentSpec(
    name="explore", description="快速代码库搜索（只读，跳过会话文件）",
    system_prompt=(
        "你是代码探索专家，只做只读搜索（read/grep/glob/bash）。\n"
        "绝不修改任何文件，不调用 write/edit。聚焦于定位代码、理解结构、"
        "汇总发现，并以简洁文本返回结果。"
    ),
    tools=["read", "grep", "glob", "bash"], disallowed_tools=["write", "edit"],
    permission_mode="plan", builtin=True,
)

BUILTIN_PLAN = AgentSpec(
    name="plan", description="plan mode 期间研究（只读）",
    system_prompt=(
        "你是研究规划专家，只做只读搜索（read/grep/glob/bash），不修改文件。\n"
        "基于探索结果产出清晰、可执行的计划（步骤、风险、依赖），以文本返回。"
    ),
    tools=["read", "grep", "glob", "bash"], disallowed_tools=["write", "edit"],
    permission_mode="plan", builtin=True,
)

BUILTIN_GENERAL = AgentSpec(
    name="general-purpose", description="复杂多步骤（探索+修改）",
    system_prompt=(
        "你是通用执行 agent，可探索与修改代码。\n"
        "先理解任务与上下文，再按需调用工具推进；完成后以简洁文本总结成果。"
    ),
    tools=None, builtin=True,
)

BUILTIN_SPECS: tuple[AgentSpec, ...] = (BUILTIN_EXPLORE, BUILTIN_PLAN, BUILTIN_GENERAL)


# --------------------------------------------------------------------------- #
# _SubAgentTransport：子任务视图渲染 + 屏蔽独立 HITL
# --------------------------------------------------------------------------- #
class _SubAgentTransport(TerminalTransport):
    """子 agent 传输：继承 TerminalTransport 获得完整渲染能力，但：

    - 绑定独立（子）EventStream，事件以固定高度滚动框（Panel）渲染到同一终端，
      不写入/混入父 agent 自己的 EventStream。
    - 屏蔽独立 HITL：澄清/审批**不弹出子 agent 自己的交互**，而是委托给 ``parent``
      传输由父代理统一决策（``parent`` 为 None 或非交互时，澄清抛错、审批自动放行）。
    """

    def __init__(
        self, parent: "AgentTransport | None", *,
        name: str = "subagent", panel_height: int = 15,
    ) -> None:
        interactive = bool(parent.interactive) if parent is not None else False
        super().__init__(interactive=interactive)
        self._parent = parent
        self._name = name
        self._panel_height = max(1, panel_height)
        self._sub_live: Live | None = None
        # 交互模式下用 record console 捕获所有 rich 输出，供框内刷新
        if interactive:
            from rich.console import Console as _RichConsole
            self._console = _RichConsole(record=True)

    @property
    def interactive(self) -> bool:
        return bool(self._parent.interactive) if self._parent is not None else False

    def bind(self, stream) -> None:
        if self.interactive:
            self._sub_live = Live(
                Panel(
                    "(等待子 agent 输出…)",
                    title=f"▶ subagent: {self._name}",
                    border_style="dim",
                    height=self._panel_height,
                ),
                console=self._console,
                auto_refresh=False,
            )
            self._sub_live.start()
        super().bind(stream)
        # 额外订阅：每个事件后刷新滚动框
        if self.interactive:
            stream.subscribe(lambda _: self._refresh_sub_live())

    def close(self) -> None:
        if self._sub_live is not None:
            self._sub_live.stop()
            self._sub_live = None
        super().close()

    def _refresh_sub_live(self) -> None:
        """从 record console 取文本，截取最后 panel_height 行刷新 Live 面板。"""
        if self._sub_live is None:
            return
        text = self._console.export_text()
        lines = text.splitlines()
        if len(lines) > self._panel_height:
            lines = lines[-self._panel_height:]
        self._sub_live.update(
            Panel(
                "\n".join(lines) if lines else "(等待子 agent 输出…)",
                title=f"▶ subagent: {self._name}",
                border_style="blue",
                height=self._panel_height,
            )
        )
        self._sub_live.refresh()

    async def ask(self, question) -> str:
        if self._parent is not None and self._parent.interactive:
            return await self._parent.ask(question)
        raise RuntimeError("subagent 不应触发独立澄清交互（HITL 由父代理统一决策）")

    async def approve(self, action) -> bool:
        if self._parent is not None and self._parent.interactive:
            return await self._parent.approve(action)
        # 非交互（或 parent 为 None）：交给调用方配置的 gate 非交互默认放行
        return True


# --------------------------------------------------------------------------- #
# SubagentSpawner
# --------------------------------------------------------------------------- #
class SubagentSpawner:
    def __init__(self, settings: Settings, *, tracer: Tracer | None = None, max_depth: int = 5) -> None:
        self.settings = settings
        self.tracer = tracer
        self.max_depth = max_depth

    # ------------------------------------------------------------------ #
    # 发现 / 获取
    # ------------------------------------------------------------------ #
    def discover(self) -> list[AgentSpec]:
        """扫描 <project>/.agent/agents/*.md 与 ~/.agent/agents/*.md（项目级覆盖同名）。

        内置类型始终可用；用户级可覆盖内置同名，项目级再覆盖用户级。
        """
        specs: dict[str, AgentSpec] = {b.name: b for b in BUILTIN_SPECS}

        user_dir = user_config_path().parent / "agents"
        project_dir = project_config_path().parent / "agents"
        for d in (user_dir, project_dir):  # 后写的覆盖先写的
            if d.is_dir():
                for f in sorted(d.glob("*.md")):
                    spec = self._parse_agent_file(f)
                    if spec is not None:
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

    def summaries(self) -> list["AgentSummary"]:
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
        parent_transport: "AgentTransport | None" = None,
        parent_messages: list[Message] | None = None,
        parent_sandbox: Any | None = None,
        parent_gate: ApprovalGate | None = None,
    ) -> AgentResult:
        """构造独立 AgentLoop（独立 EventStream + fork 可选），跑 run()，返回摘要。"""
        if depth >= self.max_depth:
            raise RecursionError(f"subagent depth limit {self.max_depth} reached")

        # ① 工具白名单：base_registry 子集
        sub_reg = self._subset_registry(base_registry or default_registry, spec)
        # ② 模型降级：spec.model 覆盖
        sub_model = self._resolve_model(base_model, spec)
        # ③ 沙箱/权限：permission_mode 映射（plan→read-only + 跳过 exec gate）
        sub_sandbox, sub_gate = self._resolve_security(spec, parent_sandbox, parent_gate)
        # ④ fork：share_history=True 时继承父 conv
        initial = list(parent_messages) if (spec.share_history and parent_messages) else []

        sub_transport = _SubAgentTransport(
            parent=parent_transport, name=spec.name,
            panel_height=spec.panel_height,
        )

        # max_turns 限制：克隆 settings 覆盖循环上限
        sub_settings = self.settings
        if spec.max_turns is not None:
            sub_settings = self.settings.model_copy(deep=True)
            sub_settings.loop.max_iterations = spec.max_turns

        loop = AgentLoop(
            sub_model, sub_reg, sub_settings,
            tracer=self.tracer, sandbox=sub_sandbox, gate=sub_gate,
        )
        # 让子 loop 继承当前深度，使嵌套 spawn 能正确累加（depth+1 传入）
        loop._current_depth = depth
        result = await loop.run(
            task,
            messages=initial,
            transport=sub_transport,
            system_prompt=spec.system_prompt or None,
            parent_span=parent_span,
        )
        return result

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
            sandbox = build_executor(
                "local", workspace=Path.cwd(), profile=SandboxProfile.READ_ONLY
            )
            gate = ApprovalGate("never")
            return sandbox, gate
        # 默认：继承父 sandbox/gate；无父则用 settings 默认
        if parent_sandbox is not None:
            return parent_sandbox, parent_gate  # type: ignore[return-value]
        sandbox = build_executor(
            "local", workspace=Path.cwd(),
            profile=SandboxProfile(self.settings.sandbox.profile),
        )
        gate = ApprovalGate(self.settings.approval.mode)
        return sandbox, gate
