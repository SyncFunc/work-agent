"""M9.2 协议契约测试：防止 Python（agent/daemon/protocol.py）与
TS（desktop/src/protocol/types.ts）的 MsgType 集合漂移。

机制：desktop/scripts/check-msgtype.mjs 纯 Node 脚本同时解析两个源文件、
比对 MsgType 集合；本测试用 subprocess 调它，断言返回码 0。

漂移场景：
- 改 Python MsgType（增/删/改值）→ node 脚本比对 types.ts 不符 → 失败。
- 改 TS ALL_MSG_TYPES（增/删/改值）→ 与 protocol.py 不符 → 失败。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent.daemon.protocol import MsgType

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / 'desktop' / 'scripts' / 'check-msgtype.mjs'

NODE = None
for candidate in ('node', 'node.exe'):
    import shutil

    NODE = shutil.which(candidate)
    if NODE:
        break


@pytest.mark.skipif(NODE is None, reason='未找到 node 可执行文件，跳过协议契约测试')
def test_msgtype_contract_node_script_passes() -> None:
    """node 脚本比对 Python 与 TS 两端 MsgType 集合一致。"""
    result = subprocess.run(
        [NODE, str(CHECK_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding='utf-8',
    )
    assert result.returncode == 0, (
        f'MsgType 契约不一致：\n{result.stdout}\n{result.stderr}'
    )


def test_python_msgtype_nonempty_and_sorted_stable() -> None:
    """Python 端 MsgType 枚举自身合理（回归护栏）。"""
    values = [m.value for m in MsgType]
    assert len(values) == 32
    assert 'hello' in values
    assert 'event' in values
    assert 'error' in values
    # 无重复值
    assert len(set(values)) == len(values)
