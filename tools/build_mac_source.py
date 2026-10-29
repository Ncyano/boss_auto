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
INCLUDE_DIRS = ["boss_vision"]
SKIP_PARTS = {"__pycache__", ".venv", "runs", "_test_runs", "assets", "data"}
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
