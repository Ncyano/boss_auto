"""打包发布：把可分发文件收进 dist/ 并生成 zip。

用法：
    .venv\\Scripts\\python.exe tools\\build_package.py
    .venv\\Scripts\\python.exe tools\\build_package.py --with-venv   (体积大但免安装依赖)

产物：
    dist\\BOSS招聘助手-<日期>\\      解压即用的目录
    dist\\BOSS招聘助手-<日期>.zip   发给别人的包
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

# 这些不进包
EXCLUDE_DIRS = {
    ".git", ".venv", "dist", "__pycache__", ".pytest_cache", ".idea",
    "runs", "_revert_backup", ".tmp", ".deps", "wheels", "tests",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip"}

# 必须包含的东西
INCLUDE = [
    "boss_vision", "运营助理知识库_v0.1", "tools",
    "config.example.json",
    "requirements.txt", "安装.cmd", "run-gui.cmd", "run-edge-debug.cmd",
    "全国普通高等学校名单.xls", "全国成人高等学校名单.xls",
    "README.md", "使用说明.md",
]


def copy_item(src: Path, dst: Path, with_venv: bool) -> None:
    if not src.exists():
        return
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIX:
            continue
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-venv", action="store_true",
                    help="把 .venv 一起打包（约 +280MB，目标机不用装依赖）")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d")
    name = f"BOSS招聘助手-{stamp}"
    out = DIST / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for item in INCLUDE:
        copy_item(REPO / item, out / item, args.with_venv)

    if args.with_venv:
        print("  复制 .venv（较慢）...")
        copy_item(REPO / ".venv", out / ".venv", True)

    # 用 example 生成模板 config.json（真实密钥不进包）
    ex = out / "config.example.json"
    if ex.exists():
        shutil.copy2(ex, out / "config.json")

    # 说明文件
    (out / "使用说明.md").write_text(
        "# BOSS 招聘助手\n\n"
        "## 首次安装\n"
        "1. 双击 `安装.cmd`（自动建虚拟环境并装依赖，需要联网）\n"
        "2. 双击 `run-edge-debug.cmd`，在弹出的浏览器里登录 BOSS\n"
        "3. 双击 `run-gui.cmd`，填目标人数，点开始\n\n"
        "## 日常使用\n"
        "- 先 `run-edge-debug.cmd`，再 `run-gui.cmd`\n"
        "- 后台模式需要调试端口，所以浏览器必须用上面的方式启动\n\n"
        "## 结果在哪\n"
        "`boss_vision\\runs\\<时间>-summary.csv`（含“已打招呼”列）\n",
        encoding="utf-8")

    # 打包 zip
    zpath = DIST / f"{name}.zip"
    if zpath.exists():
        zpath.unlink()
    print("  压缩 ...")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in out.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(DIST))

    size = zpath.stat().st_size / 1024 / 1024
    files = sum(1 for _ in out.rglob("*") if _.is_file())
    print(f"\n  目录: {out}")
    print(f"  文件: {files} 个")
    print(f"  压缩包: {zpath}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
