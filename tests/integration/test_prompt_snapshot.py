"""M6.3 Tier1 契约测试：system prompt 快照（Tier1）。

渲染**静态段**（来自 ``system.md``，确定性、不含日期/Git 等动态部分）与仓库内基线 diff；
意外改动即失败（呼应 M4.5 稳定前缀不可被无意篡改）。首次运行（基线缺失）会创建基线并
skip，需重新运行以验证；基线应随代码提交，有意变更时再更新。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.config.settings import Settings
from agent.core.prompts import _build_system_parts

_FIXTURE = Path(__file__).parent / "fixtures" / "prompts" / "system_static.json"


def test_system_prompt_static_snapshot():
    static, _ = _build_system_parts(Settings())
    assert static.strip(), "static system prompt must not be empty"

    if not _FIXTURE.exists():
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(
            json.dumps({"static": static}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        pytest.skip("baseline created; re-run to verify snapshot")

    baseline = json.loads(_FIXTURE.read_text(encoding="utf-8"))["static"]
    assert static == baseline, (
        "system prompt 静态段与基线不一致：若为有意变更请更新 "
        f"{_FIXTURE} 基线，否则说明 system.md / 渲染逻辑被意外改动。"
    )
