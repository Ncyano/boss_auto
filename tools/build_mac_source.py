"""打一个 macOS 源码包（不带 Python 运行时，需在 Mac 上跑 install-mac.sh）。"""
from __future__ import annotations

import shutil
import time
import zipfile
from pathlib import Path

ROOT = Path(".").resolve()
STAMP = time.strftime("%Y%m%d-%H%M")
NAME = f"BOSS招聘助手-macOS源码-{STAMP}"
OUT = ROOT / "dist" / f"{NAME}.zip"
OUT.parent.mkdir(exist_ok=True)

INCLUDE_FILES = [
    "install-mac.sh", "run-mac.command", "run-browser-mac.command",
    "config.example.json", "requirements.txt", "README.md", "MAC-使用说明.md",
    "pyrightconfig.json", "mcp_server.py",
]
INCLUDE_DIRS = ["boss_vision", "运营助理知识库_v0.1"]

# 运行时必需的数据文件（在仓库根 data/ 下，schools.py 与 screening.py 按此路径读取）
RUNTIME_DATA = [
    "data/schools-normalized.csv",
    "data/schools.meta.json",
    "data/schools-adult.csv",
    "data/schools-adult.meta.json",
    "data/school-aliases.json",
    "data/screening-rules.json",
]
SKIP_PARTS = {"__pycache__", ".venv", "runs", "_test_runs"}
SKIP_SUFFIX = {".pyc", ".log", ".json.bak"}

added = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.exists():
            z.write(p, f"{NAME}/{name}")
            added += 1
    for d in INCLUDE_DIRS:
        base = ROOT / d
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            if any(part in SKIP_PARTS for part in f.parts):
                continue
            if f.suffix in SKIP_SUFFIX:
                continue
            # 不放真实配置：只放示例
            if f.name == "config.json":
                continue
            z.write(f, f"{NAME}/{d}/{f.relative_to(base)}")
            added += 1
    # 运行时数据（学校名单 / 筛选规则）
    for rel in RUNTIME_DATA:
        f = ROOT / rel
        if f.exists():
            z.write(f, f"{NAME}/{rel}")
            added += 1
        else:
            print(f"  [warn] 缺少运行时数据: {rel}")

    # 内置 Python 运行时（python-build-standalone，install_only）
    mpy = ROOT / "data" / "macpython"
    if mpy.exists():
        for f in mpy.rglob("*.tar.gz"):
            rel = f.relative_to(mpy)
            z.write(f, f"{NAME}/python/{rel.as_posix()}")
            added += 1

    # 离线依赖（macOS wheel）
    wh = ROOT / "data" / "wheelhouse"
    if wh.exists():
        for f in wh.rglob("*.whl"):
            rel = f.relative_to(wh)
            z.write(f, f"{NAME}/wheelhouse/{rel.as_posix()}")
            added += 1

    # 额外：kb 数据
    kb_data = ROOT / "boss_vision" / "data"
    if kb_data.exists():
        for f in kb_data.rglob("*"):
            if f.is_file():
                z.write(f, f"{NAME}/boss_vision/data/{f.relative_to(kb_data)}")
                added += 1

size = OUT.stat().st_size / 1024
print(f"  文件数: {added}")
print(f"  输出: {OUT}")
print(f"  大小: {size:.0f} KB")
