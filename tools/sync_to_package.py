"""把仓库里的最新代码同步到打包目录（绿色版），避免打包副本一直是旧的。

用法：
    .venv\\Scripts\\python.exe tools\\sync_to_package.py
    .venv\\Scripts\\python.exe tools\\sync_to_package.py --dir "dist\\BOSS招聘助手-绿色版-20261027"

同步内容：boss_vision、运营助理知识库_v0.1、tools、config.example.json、名单 xls
不同步：config.json（保留目标机已填的密钥）、runs、__pycache__
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP = {"__pycache__", "runs", ".tmp", ".deps", "wheels", "_revert_backup", "tests"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log"}


def copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in SKIP for part in rel.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="")
    args = ap.parse_args()

    if args.dir:
        target = (REPO / args.dir).resolve()
    else:
        cands = sorted((REPO / "dist").glob("BOSS招聘助手-绿色版-*"))
        cands = [c for c in cands if c.is_dir()]
        if not cands:
            print("找不到打包目录，请先用 --dir 指定")
            return 1
        target = cands[-1]

    app = target / "app"
    if not app.exists():
        print(f"目标不是绿色版目录（没有 app/）：{app}")
        return 1

    total = 0
    for item in ("boss_vision", "运营助理知识库_v0.1", "tools"):
        src = REPO / item
        if src.exists():
            n = copy_tree(src, app / item)
            total += n
            print(f"  {item}: {n} 个文件")
    for f in ("config.example.json", "requirements.txt",
              "全国普通高等学校名单.xls", "全国成人高等学校名单.xls"):
        src = REPO / f
        if src.exists():
            shutil.copy2(src, app / f)
            print(f"  {f}: 已更新")
            total += 1

    print(f"\n  已同步 {total} 个文件 -> {app}")
    print("  注意：app\\config.json 保持不变（保留已填的密钥）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
