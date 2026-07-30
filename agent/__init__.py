"""通用编码 Agent。"""

from __future__ import annotations

import sys

__version__ = "0.3.0"

# Windows 控制台默认 cp1252 无法输出中文（UnicodeEncodeError）。
# 在包加载时统一将标准流设为 UTF-8，覆盖所有入口（daemon、CLI run/chat 等）。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (ValueError, OSError):
            pass
