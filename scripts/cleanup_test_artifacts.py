#!/usr/bin/env python3
"""清理测试/调试运行后散落在仓库根的临时产物，避免被误提交。

设计要点
--------
* 单一事实来源：需要清理的文件名 / glob 全部集中在 ``CLEANUP_PATTERNS``，
  只在仓库根目录（``REPO_ROOT``）做**直接子文件**匹配，绝不递归，避免误删源码。
* 自动运行：``tests/conftest.py`` 的 ``pytest_sessionfinish`` 钩子在每次
  ``pytest`` 结束后自动调用本脚本（排除 ``coverage.xml``，因为 CI 要上传它）。
* 手动运行：``python scripts/cleanup_test_artifacts.py [--dry-run] [--exclude NAME ...]``

维护约定（重要）
----------------
当**新增的测试用例**会在仓库根留下新的散文件时，请把对应文件名或 glob
追加到下面的 ``CLEANUP_PATTERNS`` 列表里，保持这里的清单始终是“最新真相”。
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

# Windows 默认控制台为 GBK，直接 print 中文/✓ 会抛 UnicodeEncodeError。
# 强制 stdout/stderr 走 UTF-8，保证跨平台（CI Linux 本就是 UTF-8）不崩。
# 仅在确实是 TextIOWrapper 时调用 reconfigure（sys.stdout 类型被标注为 TextIO
# 协议，没有 reconfigure；实际运行时是 TextIOWrapper，isinstance 收窄后类型正确）。
try:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 仅非 CPython/老版本兜底
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent

# 需要清理的产物：文件名 / glob（仅在 REPO_ROOT 直接子文件匹配）。
# 只列“确定是测试/调试垃圾”的模式，绝不列任何源码或配置。
CLEANUP_PATTERNS: list[str] = [
    # 覆盖率报告（CI 以 artifact 形式上传，本地勿入库）
    "coverage.xml",
    # 沙箱测试曾写入仓库根的产物（源码已改为写 tmp_path，这里兜底）
    "sandbox_test_tmp.txt",
    "f.txt",
    # 工具/调试用例留下的 ad-hoc 文件
    "a.txt",
    # 任意以 _ 开头的调试 dump（如 _m63_out.txt）
    "_*.txt",
    # 根目录随手建的调试文件
    "x",
    # 通用临时文件
    "*.tmp",
]


def collect(exclude: set[str] | None = None) -> list[Path]:
    """返回仓库根下匹配 ``CLEANUP_PATTERNS`` 且不在 ``exclude`` 中的文件列表。"""
    exclude = exclude or set()
    found: set[Path] = set()
    for pattern in CLEANUP_PATTERNS:
        if pattern in exclude:
            continue
        for p in REPO_ROOT.glob(pattern):
            if p.is_file():
                found.add(p.resolve())
    return sorted(found)


def cleanup(exclude: set[str] | None = None, *, dry_run: bool = False) -> int:
    targets = collect(exclude)
    if not targets:
        print("cleanup: 没有需要清理的散文件 ✓")
        return 0
    for p in targets:
        rel = p.relative_to(REPO_ROOT)
        if dry_run:
            print(f"  [dry-run] 将删除 {rel}")
        else:
            p.unlink()
            print(f"  已删除 {rel}")
    tag = " (dry-run)" if dry_run else ""
    print(f"cleanup: 共处理 {len(targets)} 个文件{tag}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="清理测试运行后的仓库根散文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要删除的文件，不实际删除")
    ap.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="排除指定模式（如 coverage.xml），不清理它们",
    )
    args = ap.parse_args()
    return cleanup(exclude=set(args.exclude), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
