"""打一个「目标机不需要装 Python」的绿色包。

做法：把本机 Python 运行时（python.exe + DLL + 标准库）连同已装好的第三方库
一起复制到包里，再用包内自带的解释器启动，目标机直接双击即可运行。

用法：
    .venv\\Scripts\\python.exe tools\\build_portable.py

产物：
    dist\\BOSS招聘助手-绿色版-<日期>\\      绿色目录
    dist\\BOSS招聘助手-绿色版-<日期>.zip   发给没装 Python 的机器
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".pdb"}
EXCLUDE_DIRS = {"__pycache__", "runs", "_revert_backup", ".tmp", ".deps", "wheels", "tests"}


def venv_python() -> Path:
    return REPO / ".venv" / "Scripts" / "python.exe"


def base_python() -> Path:
    cfg = (REPO / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    for line in cfg.splitlines():
        if line.startswith("home"):
            return Path(line.split("=", 1)[1].strip())
    raise RuntimeError("找不到 base python")


def copy_tree(src: Path, dst: Path, skip: set[str] | None = None) -> None:
    skip = skip or set()
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in EXCLUDE_DIRS or part in skip for part in rel.parts):
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
    stamp = time.strftime("%Y%m%d")
    name = f"BOSS招聘助手-绿色版-{stamp}"
    out = DIST / name
    if out.exists():
        shutil.rmtree(out)
    app = out / "app"
    py = out / "python"
    app.mkdir(parents=True)
    py.mkdir(parents=True)

    base = base_python()
    print(f"  基础 Python: {base}")

    # ---- 1. Python 运行时 ----
    ver = f"python{sys.version_info.major}{sys.version_info.minor}"
    for f in base.glob("python*.dll"):
        shutil.copy2(f, py / f.name)
    for f in base.glob("vcruntime*.dll"):
        shutil.copy2(f, py / f.name)
    for f in base.glob("*.exe"):
        if f.name.lower() in ("python.exe", "pythonw.exe"):
            shutil.copy2(f, py / f.name)
    print(f"  已复制 {ver}.dll / python.exe")

    # 标准库（排除第三方 site-packages，稍后用 venv 的）
    print("  复制标准库 ...")
    copy_tree(base / "Lib", py / "Lib", skip={"site-packages", "test", "idlelib",
                                              "tkinter", "turtledemo", "lib2to3"})
    # tkinter 是 GUI 必需的
    copy_tree(base / "Lib" / "tkinter", py / "Lib" / "tkinter")
    copy_tree(base / "tcl", py / "tcl")
    if (base / "DLLs").exists():
        copy_tree(base / "DLLs", py / "DLLs", skip={"tk86t.dll", "tcl86t.dll"})

    # ---- 2. 第三方库（从 venv 拿已装好的） ----
    print("  复制第三方库 ...")
    sp_src = REPO / ".venv" / "Lib" / "site-packages"
    sp_dst = py / "Lib" / "site-packages"
    sp_dst.mkdir(parents=True, exist_ok=True)
    keep_prefix = ("numpy", "cv2", "PIL", "openpyxl", "et_xmlfile", "pip", "setuptools")
    for item in sp_src.iterdir():
        if item.name.startswith("~"):
            continue
        if item.name in ("__pycache__",):
            continue
        if item.is_dir() and not item.name.startswith(keep_prefix):
            continue
        if item.is_file() and item.suffix in (".pth", ".dist-info"):
            continue
        if item.is_dir():
            copy_tree(item, sp_dst / item.name)
        else:
            shutil.copy2(item, sp_dst / item.name)

    # ---- 3. 应用本体 ----
    print("  复制应用 ...")
    for item in ("boss_vision", "运营助理知识库_v0.1", "tools"):
        copy_tree(REPO / item, app / item)
    for f in ("config.example.json", "requirements.txt",
              "全国普通高等学校名单.xls", "全国成人高等学校名单.xls"):
        src = REPO / f
        if src.exists():
            shutil.copy2(src, app / f)
    ex = REPO / "config.example.json"
    if ex.exists():
        shutil.copy2(ex, app / "config.json")      # 模板，不含真实密钥

    # ---- 4. 启动脚本 ----
    (out / "启动控制台.cmd").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "start \"BOSS Collector Console\" cmd /k \"\"%~dp0python\\python.exe\" -E \"%~dp0app\\boss_vision\\gui.py\"\"\r\n",
        encoding="utf-8")
    edge = REPO / "run-edge-debug.cmd"
    if edge.exists():
        shutil.copy2(edge, out / "启动调试浏览器.cmd")

    # ---- 5. 自检：用包内解释器导入依赖 ----
    print("  自检 ...")
    r = subprocess.run([str(py / "python.exe"), "-E", "-c",
                        "import numpy, cv2, PIL, openpyxl, tkinter; print('ok')"],
                       capture_output=True, text=True)
    print("   ", (r.stdout or r.stderr).strip()[:200])

    # ---- 6. 打包 ----
    zpath = DIST / f"{name}.zip"
    if zpath.exists():
        zpath.unlink()
    print("  压缩（较大，稍等）...")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in out.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(DIST))
    size = zpath.stat().st_size / 1024 / 1024
    print(f"\n  目录: {out}")
    print(f"  压缩包: {zpath}  ({size:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
