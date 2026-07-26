"""冻结 agentrunner daemon 为平台原生可执行文件（方案 A：脱离主机 Python 环境）。

本脚本被 CD 流水线调用，把 `agent.cli daemon` 子命令（见 agent/daemon_launcher.py）
用 PyInstaller 冻结为单一二进制：

    python scripts/build_daemon.py --dist desktop/build/daemon/windows
    python scripts/build_daemon.py --dist desktop/build/daemon/linux
    python scripts/build_daemon.py --dist desktop/build/daemon/mac

产物：
    Windows -> <dist>/daemon.exe
    Linux   -> <dist>/daemon
    macOS   -> <dist>/daemon

注意：
- 仅冻结 daemon 运行所需模块。`agent.tui`（依赖 textual）与 `agent.testing`
  （依赖 pytest）不参与分发，已通过 --exclude-module 排除，避免把开发态依赖打入产物。
- 提示词 Markdown 等数据文件通过 --collect-data agent 一并打包（运行期按包内路径读取）。
- 冻结后进程参数透传，故 `--port` 等 CLI 覆盖仍然有效。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def binary_name(platform: str) -> str:
    return "daemon.exe" if platform == "windows" else "daemon"


def build(dist_dir: Path) -> Path:
    # pyinstaller 仅属于 [bundle] 可选依赖（CD 构建环境安装），开发/CI 快门禁不装，
    # 故用 try/except + type: ignore 让 basedpyright 在无 pyinstaller 时也能通过。
    try:
        from PyInstaller.main import run  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - 仅在 `pip install -e ".[bundle]"` 后可用
        raise RuntimeError(
            '构建冻结 daemon 需要 pyinstaller，请执行 `pip install -e ".[bundle]"`。',
        ) from None

    dist_dir = dist_dir.resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    entry = str(ROOT / "agent" / "daemon_launcher.py")

    # PyInstaller 的 argv 形态：扁平列表，每个 --flag 后跟其参数（如有）。
    cmd: list[str] = [
        entry,
        "--name",
        "daemon",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--paths",
        str(ROOT),
        "--collect-data",
        "agent",
        "--exclude-module",
        "agent.tui",
        "--exclude-module",
        "agent.testing",
        "--hidden-import",
        "openai",
        "--hidden-import",
        "websockets",
        "--hidden-import",
        "pydantic_settings",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(dist_dir / "_work"),
        "--specpath",
        str(dist_dir / "_spec"),
    ]

    print(f"[build_daemon] PyInstaller 入口: {entry}")
    print(f"[build_daemon] 产物目录: {dist_dir}")
    run(cmd)

    # PyInstaller 在 <distpath>/<name> 产出二进制（onefile 直接落在 distpath 根）。
    produced = dist_dir / "daemon"
    if not produced.exists():
        # Windows 下扩展名为 .exe
        produced = dist_dir / "daemon.exe"
    if not produced.exists():
        raise FileNotFoundError(
            f"未找到冻结产物：{dist_dir / 'daemon'} / {dist_dir / 'daemon.exe'}"
        )
    return produced


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze agentrunner daemon with PyInstaller")
    parser.add_argument(
        "--dist",
        required=True,
        help="产物输出目录，例如 desktop/build/daemon/windows",
    )
    args = parser.parse_args()

    produced = build(Path(args.dist))
    print(f"[build_daemon] 完成：{produced}")


if __name__ == "__main__":
    sys.exit(main())
