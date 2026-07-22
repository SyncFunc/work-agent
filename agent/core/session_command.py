"""M7.5 命令分发（进程内 run/chat 与 daemon 协议路径共用，单一来源）。

把 ``cli._chat_repl`` 内的 ``/`` 命令分发逻辑抽为共享函数，避免协议路径重复实现命令语义。

- ``dispatch_command(session, raw, transport, settings, *, feedback=None) -> bool``
  - ``raw``：用户输入的原始行（可能以 ``/`` 开头）。
  - 返回 ``True`` 表示这是一条已处理的命令（调用方不应再当普通任务发往模型）；
    返回 ``False`` 表示非命令（调用方按普通任务处理）。
  - ``feedback``：反馈输出回调（cli 用 ``typer.echo(..., err=True)``；daemon 用 ``transport.notify``）。
- 仅处理 ``/`` 命令；``exit`` / ``quit`` 由调用方（REPL）先行拦截，不在此处理。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Callable

from agent.core.model import Message

if TYPE_CHECKING:
    from agent.config.settings import Settings
    from agent.core.session import SessionLike
    from agent.core.transport import AgentTransport


async def dispatch_command(
    session: "SessionLike",
    raw: str,
    transport: "AgentTransport",
    settings: "Settings",
    *,
    feedback: "Callable[[str], None] | None" = None,
) -> bool:
    """分发一条 ``/`` 命令；返回 True=已处理（含未知 slash 命令）。

    为支持 ``/compact`` 的 ``await compact()``，本函数为异步，调用方需 ``await``。
    """
    cmd = raw.strip().lower()
    if not cmd.startswith("/"):
        return False

    fb = feedback or _default_feedback()

    # ---- 模式切换 ----
    if cmd in {"/plan"}:
        session.plan_mode = True
        fb("→ 已切换到 PLAN 模式（探索，不修改任何文件）")
        return True
    if cmd in {"/exec"}:
        session.plan_mode = False
        if session.plan_path is None and os.path.isfile(settings.plan.file):
            session.plan_path = settings.plan.file
        fb("→ 已切换到 EXEC 模式（可执行）")
        return True
    if cmd in {"/approve"}:
        if session.plan_path is None and os.path.isfile(settings.plan.file):
            session.plan_path = settings.plan.file
        if session.plan_path:
            session.plan_mode = False
            fb(f"→ 已批准计划并切到 EXEC 模式：{session.plan_path}")
        else:
            fb("→ 当前没有待批准的计划（先用 /plan 让模型产出计划）")
        return True
    if cmd in {"/mode"}:
        fb(
            f"→ 当前模式：{'PLAN' if session.plan_mode else 'EXEC'}"
            + (f"，计划：{session.plan_path}" if session.plan_path else "")
        )
        return True

    # ---- 上下文命令 ----
    if cmd in {"/context"}:
        if session.context_mgr is not None:
            fb(session.context_mgr.format_usage())
        else:
            fb("→ 上下文管理未启用（settings.context.*_enabled 全为 false）")
        return True
    if cmd in {"/compact"}:
        if session.context_mgr is not None:
            fb("→ 正在压缩上下文...")
            ok = await session.context_mgr.compact()
            if ok:
                session.messages = session.context_mgr.conv
                usage = session.context_mgr.estimate_usage()
                fb(
                    f"✅ 压缩完成。当前占用：{usage.total:,} / "
                    f"{session.context_mgr.effective_window:,} ({usage.used_pct:.1%})"
                )
            else:
                fb("⚠️ 压缩未执行（上下文尚小或连续失败已放弃）")
        else:
            fb("→ 上下文管理未启用（settings.context.*_enabled 全为 false）")
        return True

    # ---- Skill / Subagent 命令 ----
    if cmd in {"/skills"}:
        transport.show_skills(session.list_skills())
        return True
    if cmd in {"/agents"}:
        transport.show_agents(session.list_agents())
        return True
    if cmd in {"/skill"}:
        fb("用法: /skill <name>  —— 显式加载某 skill 到下一轮对话")
        return True
    if cmd.startswith("/skill "):
        name = raw.strip()[len("/skill "):].strip()  # 保留原名大小写
        if session.skill_loader is None:
            fb("skills 未启用（settings.skills.enabled=false）")
        else:
            spec = session.skill_loader.get(name)
            if spec is None:
                fb(f"未找到 skill: {name}")
            else:
                session.messages.append(
                    Message(role="user", content=f"[Skill {name}]\n{spec.render_body()}")
                )
                fb(f"已加载 skill: {name}")
        return True

    # ---- 后台 Subagent 命令 ----
    if cmd in {"/agent"}:
        fb("用法: /agent <name> <task>  —— 后台启动一个 Subagent")
        return True
    if cmd.startswith("/agent "):
        rest = raw.strip()[len("/agent "):].strip()
        space_idx = rest.find(" ")
        if space_idx < 0:
            fb("用法: /agent <name> <task>  —— name 和 task 之间用空格分隔")
        else:
            agent_name = rest[:space_idx]
            agent_task = rest[space_idx + 1:].strip()
            if not agent_task:
                fb("用法: /agent <name> <task>  —— task 不能为空")
            else:
                task_id = session.spawn_background(
                    agent_name,
                    agent_task,
                    transport,
                    parent_span=session.loop._agent_span,
                )
                if task_id is not None:
                    fb(
                        f"→ 后台 Subagent [{agent_name}] 已启动（task_id: {task_id}），"
                        f"完成后将自动通知。可用 /bg 查看状态。"
                    )
        return True
    if cmd in {"/bg"}:
        tasks = session.list_background_tasks()
        if not tasks:
            fb("→ 当前没有运行中的后台任务")
        else:
            fb(f"→ 后台任务数: {len(tasks)}")
            for t in tasks:
                status = "✅ 已完成" if t["done"] else "🔄 运行中"
                fb(f"  {t['id']}: {status}")
        return True

    # 未知 slash 命令：仍视为已处理（避免误当作任务发往模型）
    fb(f"未知命令: {raw.strip()}")
    return True


def _default_feedback() -> Callable[[str], None]:
    from typer import echo

    return lambda m: echo(m, err=True)
