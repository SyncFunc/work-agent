"""内置工具集合。导入本模块即把内置工具登记到默认注册表。

- ``bash`` / ``fs``：核心执行与文件工具。
- ``explore``：只读探索工具（glob / find / list_dir / fetch_url）。
"""

from agent.tools import bash, explore, fs  # noqa: F401  (side-effect: 注册工具)

__all__ = ["bash", "explore", "fs"]
