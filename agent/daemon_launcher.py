"""冻结 daemon 的专用入口（仅供 PyInstaller 打包，不参与本地开发路径）。

本地开发仍走 `python -m agent.cli daemon`；本模块仅在 CD 流水线把 daemon 冻结为
独立可执行文件后，由 Electron 在**打包态**拉起。它等价于调用 `agent.cli` 的
`daemon` 子命令，并把进程参数原样透传给 typer，从而保留 `--port` 等覆盖能力。

冻结产物即一个自包含二进制的 `agent.cli daemon`，无需主机安装 Python 环境。
"""

from __future__ import annotations

import sys

from agent.cli import app


def main() -> None:
    # Typer 继承 click，可直接以参数列表调用指定子命令；透传 sys.argv[1:] 以保留 --port 等。
    app(args=["daemon", *sys.argv[1:]])


if __name__ == "__main__":
    main()
