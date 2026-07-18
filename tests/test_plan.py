"""M1.4 验收：PLAN 模式（计划落盘 + 进度更新 + 风险门控 + 事件往返）。

覆盖（详见 milestones/M1-骨架/1.4-PLAN模式.md）：
- plan 模式：FakeModel 调 present_plan → 计划文件生成（正文 + 步骤），提前返回，无 mutating 工具执行。
- plan 模式误调写工具 → 被风险门控拦截（ToolResult(ok=False, 含 "plan mode blocks")），循环不崩。
- 执行期（plan_path 已知）调 update_plan → 计划文件步骤状态被改写、emit plan_progress。
- 非 plan 模式：同一脚本正常执行写工具、不进入 plan 提前返回。
- PlanStore 渲染↔解析对称；plan / plan_progress 事件 JSON 往返保真。
全程 FakeModel，不依赖真实 LLM。
"""

import os

from agent.config.settings import Settings
from agent.core.control_tools import (
    PRESENT_PLAN_TOOL_NAME,
    UPDATE_PLAN_TOOL_NAME,
)
from agent.core.events import Event, EventStream
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.plan import Plan, PlanStore, PlanStep
from agent.runtime.registry import ToolRegistry, ToolResult, tool
from agent.tools.fs import read, write


async def _echo(args: dict) -> ToolResult:
    return ToolResult(ok=True, output=str(args))


def _settings(**kw) -> Settings:
    base = dict(max_iterations=20, max_tool_concurrency=5, max_repeat_calls=3, clarify_enabled=False)
    base.update(kw)
    return Settings(**base)


def _make_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(read)
    r.register(write)
    r.register(tool("echo", risk="read")(_echo))
    return r


def _pp(body: str, steps: list[dict]) -> Decision:
    return Decision(tool_calls=[
        ToolCall(id="p1", name=PRESENT_PLAN_TOOL_NAME, arguments={"body": body, "steps": steps})
    ])


async def test_plan_mode_writes_plan_file(tmp_path):
    settings = _settings(plan_mode=True, plan_file=str(tmp_path / "plan.md"))
    model = FakeModel([_pp("目标：建骨架", [{"id": "S1", "title": "模块"}, {"id": "S2", "title": "测试"}])])
    res = await AgentLoop(model, _make_registry(), settings).run("设计任务")

    assert res.needs_plan_confirm is True
    assert res.plan_path and os.path.isfile(res.plan_path)
    text = open(res.plan_path, encoding="utf-8").read()
    assert "目标：建骨架" in text and "## Steps" in text
    assert res.plan_steps and len(res.plan_steps) == 2
    # 计划提前返回：不应产生任何 tool_use 事件（present_plan 是虚拟工具，不进事件流）
    assert [e for e in res.events if e.type == "tool_use"] == []


async def test_plan_mode_blocks_mutating_tool(tmp_path):
    settings = _settings(plan_mode=True, plan_file=str(tmp_path / "plan.md"))
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="w1", name="write", arguments={"path": "x", "content": "y"})]),
        _pp("body", [{"id": "S1", "title": "t"}]),
    ])
    res = await AgentLoop(model, _make_registry(), settings).run("写点东西")

    assert res.needs_plan_confirm is True  # 第二次决策转为计划，循环未崩
    tr = next(e for e in res.events if e.type == "tool_result")
    assert tr.tool_result is not None and not tr.tool_result.ok
    assert "plan mode blocks" in (tr.tool_result.error or "")


async def test_exec_mode_update_plan_rewrites(tmp_path):
    plan_file = str(tmp_path / "plan.md")
    # 先落盘计划（plan 模式）
    m1 = FakeModel([_pp("body", [{"id": "S1", "title": "t"}])])
    await AgentLoop(m1, _make_registry(), _settings(plan_mode=True, plan_file=plan_file)).run("计划")

    # 执行期：update_plan(S1, in_progress) → final
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="u1", name=UPDATE_PLAN_TOOL_NAME,
                                      arguments={"step_id": "S1", "status": "in_progress"})]),
        Decision(text="done"),
    ])
    loop = AgentLoop(model, _make_registry(), _settings(plan_file=plan_file),
                     plan_mode=False, plan_path=plan_file)
    res = await loop.run("执行")

    assert res.text == "done"
    prog = [e for e in res.events if e.type == "plan_progress"]
    assert prog and prog[0].plan_update == {"step_id": "S1", "status": "in_progress", "note": None}
    text = open(plan_file, encoding="utf-8").read()
    assert "- [~] S1" in text  # in_progress 标记


async def test_non_plan_mode_executes_write(tmp_path):
    settings = _settings(plan_mode=False, plan_file=str(tmp_path / "plan.md"))
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="w1", name="write", arguments={"path": "a.txt", "content": "hi"})]),
        Decision(text="wrote"),
    ])
    res = await AgentLoop(model, _make_registry(), settings).run("写文件")

    assert res.text == "wrote"
    tr = next(e for e in res.events if e.type == "tool_result")
    assert tr.tool_result is not None and tr.tool_result.ok


async def test_mode_switchable_per_run(tmp_path):
    """plan/exec 模式是每次 run 的可覆盖入参，可在任意轮次自由切换（M 用户诉求）。"""
    plan_file = str(tmp_path / "plan.md")
    reg = _make_registry()
    settings = _settings(plan_file=plan_file)
    loop = AgentLoop(FakeModel([]), reg, settings)  # 模式每次 run 显式传入，构造期缺省不约束

    # 轮次 1：PLAN 模式 → 产出计划并提前返回
    loop.model = FakeModel([_pp("body", [{"id": "S1", "title": "t"}])])
    r1 = await loop.run("设计", plan_mode=True, plan_path=None)
    assert r1.needs_plan_confirm and r1.plan_path

    # 轮次 2：切到 EXEC 模式（带已批准计划）→ update_plan 回写进度
    loop.model = FakeModel([
        Decision(tool_calls=[ToolCall(id="u", name=UPDATE_PLAN_TOOL_NAME,
                                      arguments={"step_id": "S1", "status": "in_progress"})]),
        Decision(text="done"),
    ])
    r2 = await loop.run("执行", r1.messages, plan_mode=False, plan_path=r1.plan_path)
    assert r2.text == "done"
    assert any(e.type == "plan_progress" for e in r2.events)

    # 轮次 3：任意切回 PLAN 模式 → 再次 present_plan（探索，不执行）
    loop.model = FakeModel([_pp("body2", [{"id": "S1", "title": "t2"}])])
    r3 = await loop.run("再设计", r2.messages, plan_mode=True, plan_path=r2.plan_path)
    assert r3.needs_plan_confirm
    assert "body2" in (r3.plan or "")


def test_planstore_roundtrip(tmp_path):
    plan = Plan(body="正文", steps=[
        PlanStep(id="S1", title="一"),
        PlanStep(id="S2", title="二", status="done"),
    ])
    PlanStore.write_plan(plan, str(tmp_path / "plan.md"))
    back = PlanStore.read_plan(str(tmp_path / "plan.md"))
    assert back.body == "正文"
    assert [(s.id, s.title, s.status) for s in back.steps] == [
        ("S1", "一", "pending"), ("S2", "二", "done"),
    ]


# --------------------------------------------------------------------------- #
# PLAN 模式 bash 只读命令白名单（M1.4 增强）
# --------------------------------------------------------------------------- #
def test_is_readonly_command():
    """bash 只读命令判定：放行的只读命令 vs 拦截的写命令。"""
    from agent.tools.bash import is_readonly_command

    allow = ["ls", "git status", "echo", "grep"]
    # 放行
    assert is_readonly_command("ls -la", allow)
    assert is_readonly_command("git status", allow)
    assert is_readonly_command("git status --short", allow)
    assert is_readonly_command("FOO=bar ls -la", allow)        # 环境变量赋值前缀
    assert is_readonly_command("sudo ls -la", allow)           # sudo 前缀
    assert is_readonly_command("ls -la | grep x", allow)       # 管道到只读
    # 拦截（写 / 不在白名单）
    assert not is_readonly_command("rm -rf x", allow)
    assert not is_readonly_command("git push", allow)
    assert not is_readonly_command("echo hi > file.txt", allow)  # 输出重定向写
    assert not is_readonly_command("ls -la && rm x", allow)      # 链式含写


async def test_plan_mode_allows_readonly_bash(tmp_path):
    """PLAN 模式下只读 bash（如 ls）应放行执行，而非被风险门控拦截。"""
    from agent.tools.bash import bash as bash_spec

    reg = _make_registry()
    reg.register(bash_spec)
    settings = _settings(plan_mode=True, plan_file=str(tmp_path / "plan.md"))
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "ls -la"})]),
        Decision(text="done exploring"),
    ])
    res = await AgentLoop(model, reg, settings).run("探索目录")

    tr = next(e for e in res.events if e.type == "tool_result")
    assert tr.tool_result is not None and tr.tool_result.ok, tr.tool_result
    assert res.text == "done exploring"


async def test_plan_mode_allows_find_command(tmp_path):
    """PLAN 模式下 find（只读探索搜索）应放行，不报 mutating bash。

    回归：find 在 plan_mode_bash_allow 默认白名单中；带 || echo 兜底、
    -not -path 与 2>/dev/null 的重定向也应判定为只读而放行。
    """
    from agent.tools.bash import bash as bash_spec

    reg = _make_registry()
    reg.register(bash_spec)
    settings = _settings(plan_mode=True, plan_file=str(tmp_path / "plan.md"))
    model = FakeModel([
        Decision(tool_calls=[ToolCall(
            id="f1", name="bash",
            arguments={"cmd": 'find . -name "*.py" -not -path "./.git/*" -type f 2>/dev/null || echo "NOT_FOUND"'},
        )]),
        Decision(text="found files"),
    ])
    res = await AgentLoop(model, reg, settings).run("找 py 文件")
    tr = next(e for e in res.events if e.type == "tool_result")
    assert tr.tool_result is not None and tr.tool_result.ok, tr.tool_result
    assert res.text == "found files"


async def test_plan_mode_blocks_mutating_bash(tmp_path):
    """PLAN 模式下可变 bash（如 rm -rf）仍被风险门控拦截，循环不崩。"""
    from agent.tools.bash import bash as bash_spec

    reg = _make_registry()
    reg.register(bash_spec)
    settings = _settings(plan_mode=True, plan_file=str(tmp_path / "plan.md"))
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "rm -rf /"})]),
        _pp("body", [{"id": "S1", "title": "t"}]),
    ])
    res = await AgentLoop(model, reg, settings).run("探索")

    assert res.needs_plan_confirm is True  # 第二次决策转为计划，循环未崩
    blocked = next(
        e for e in res.events
        if e.type == "tool_result" and e.tool_result is not None and not e.tool_result.ok
    )
    assert "plan mode blocks mutating bash" in (blocked.tool_result.error or "")


def test_plan_events_roundtrip():
    es = EventStream()
    es.append(Event(type="plan", text="b", plan_path="/tmp/p.md"))
    es.append(Event(type="plan_progress", plan_path="/tmp/p.md",
                    plan_update={"step_id": "S1", "status": "done", "note": "ok"}))
    rebuilt = EventStream.from_json(es.to_json())

    types = [e.type for e in rebuilt]
    assert "plan" in types and "plan_progress" in types
    pp = next(e for e in rebuilt if e.type == "plan_progress")
    assert pp.plan_path == "/tmp/p.md"
    assert pp.plan_update == {"step_id": "S1", "status": "done", "note": "ok"}


class _FakeUI:
    """最小 SessionUI 替身：confirm_plan 为 async（复现修复前嵌套事件循环崩溃点）。"""

    def __init__(self, confirm: bool = True) -> None:
        self.interactive = True
        self.confirm = confirm
        self.shown_plan = False

    async def ask(self, question):
        return ""

    def show_questions(self, questions):
        pass

    def show_plan(self, res):
        self.shown_plan = True

    async def confirm_plan(self) -> bool:
        return self.confirm

    def notify(self, message):
        pass


async def test_exec_turn_gets_update_plan_after_present(tmp_path):
    """回归 M1.4+chat 修复：PLAN 模式 present_plan 并经（async）确认批准后，
    EXEC 续跑轮次下发给模型的工具应包含 update_plan，且确认不触发
    'asyncio.run() cannot be called from a running event loop'。"""
    from agent.core.session import Session

    plan_file = str(tmp_path / "plan.md")
    settings = _settings(plan_mode=True, plan_file=plan_file)
    model = FakeModel([
        _pp("目标：写计划", [{"id": "S1", "title": "t"}]),   # run1：present_plan
        Decision(tool_calls=[ToolCall(
            id="u1", name=UPDATE_PLAN_TOOL_NAME,
            arguments={"step_id": "S1", "status": "in_progress"})]),
        Decision(text="完成"),                                 # run2 续跑：update_plan → final
    ])
    session = Session(model, _make_registry(), settings, plan_mode=True)
    res, err = await session.step("做一个计划", _FakeUI(confirm=True),
                                  yes=False, fatal_plan_decline=False)

    assert err is None
    assert res.text == "完成"
    # present 后（批准前）即记录 plan_path，使 EXEC 轮次可按其下发 update_plan
    assert session.plan_path is not None
    # 第二次 act（EXEC 模式）下发给模型的工具应包含 update_plan
    exec_tools = model.tools_seen[1]
    names = [t["function"]["name"] for t in exec_tools]
    assert UPDATE_PLAN_TOOL_NAME in names
    # 计划步骤被 update_plan 改写为 in_progress
    plan = PlanStore.read_plan(session.plan_path)
    assert plan.steps[0].status == "in_progress"


async def test_plan_present_records_path_even_if_declined(tmp_path):
    """present_plan 后即便用户拒绝批准，也应记录 plan_path（不崩溃），
    保持 PLAN 模式、不丢已知计划。"""
    from agent.core.session import Session

    plan_file = str(tmp_path / "plan.md")
    settings = _settings(plan_mode=True, plan_file=plan_file)
    model = FakeModel([_pp("目标：写计划", [{"id": "S1", "title": "t"}])])
    session = Session(model, _make_registry(), settings, plan_mode=True)
    res, err = await session.step("做一个计划", _FakeUI(confirm=False),
                                  yes=False, fatal_plan_decline=False)

    assert err is None
    assert session.plan_path is not None            # 已记录，供后续 /exec 启用 update_plan
    assert session.plan_mode is True                # 拒绝 → 保持 PLAN 模式
    assert res.needs_plan_confirm is True
