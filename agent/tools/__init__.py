"""内置工具集合。导入本模块即把 read/write/bash 登记到默认注册表。"""

from agent.tools import bash, fs  # noqa: F401  (side-effect: 注册工具)

__all__ = ["bash", "fs"]
