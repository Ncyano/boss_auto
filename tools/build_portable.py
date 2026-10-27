"""打「目标机不需要装 Python」的绿色包（自包含，可直接拷走）。

用法：
    .venv\\Scripts\\python.exe tools\\build_portable.py

产物：
    dist\\BOSS招聘助手-绿色版-<日期>\\      绿色目录（可整个拷到别的机器）
    dist\\BOSS招聘助手-绿色版-<日期>.zip   发给目标机
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

SKIP_DIRS = {"__pycache__", "runs", ".tmp", ".deps", "wheels", "_revert_backup", "tests"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".pdb"}
KEEP_PKG = ("numpy", "cv2", "PIL", "openpyxl", "et_xmlfile", "pip",
            "rapidocr", "onnxruntime", "pyclipper", "shapely", "yaml",
            "six", "sympy", "packaging", "coloredlogs", "humanfriendly",
            "flatbuffers", "protobuf", "google", "mpmath", "networkx",
            "markdown_it", "mdurl", "psutil", "requests", "urllib3",
            "charset_normalizer", "idna", "certifi")


def base_python() -> Path:
    cfg = (REPO / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    for line in cfg.splitlines():
        if line.startswith("home"):
            return Path(line.split("=", 1)[1].strip())
    raise RuntimeError("找不到 base python")


def copy_tree(src: Path, dst: Path, extra_skip: set[str] | None = None) -> int:
    n = 0
    skip = SKIP_DIRS | (extra_skip or set())
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in skip for part in rel.parts):
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
    stamp = time.strftime("%Y%m%d")
    name = f"BOSS招聘助手-绿色版-{stamp}-{time.strftime('%H%M')}"
    out = DIST / name
    if out.exists():
        shutil.rmtree(out)
    app, py = out / "app", out / "python"
    app.mkdir(parents=True)
    py.mkdir(parents=True)

    base = base_python()
    print(f"  基础 Python: {base}")

    # 1) 解释器与运行时
    for pat in ("python*.dll", "vcruntime*.dll", "python.exe", "pythonw.exe"):
        for f in base.glob(pat):
            shutil.copy2(f, py / f.name)
    print("  python.exe / dll 已复制")

    # 2) 标准库（含 tkinter）与扩展模块
    print("  复制标准库 ...")
    copy_tree(base / "Lib", py / "Lib",
              extra_skip={"site-packages", "test", "idlelib", "turtledemo", "lib2to3"})
    copy_tree(base / "Lib" / "tkinter", py / "Lib" / "tkinter")
    copy_tree(base / "DLLs", py / "DLLs")
    print("  复制 Tcl/Tk ...")
    for sub in ("tcl8.6", "tk8.6", "tcl8"):
        src = base / "Library" / "lib" / sub
        if src.exists():
            copy_tree(src, py / "tcl" / sub)

    # 3) 第三方库（来自 venv，已装好）
    print("  复制第三方库 ...")
    sp_src = REPO / ".venv" / "Lib" / "site-packages"
    sp_dst = py / "Lib" / "site-packages"
    sp_dst.mkdir(parents=True, exist_ok=True)
    for item in sp_src.iterdir():
        if item.name.startswith("~") or item.name == "__pycache__":
            continue
        if item.is_dir():
            if item.name.startswith(KEEP_PKG):
                copy_tree(item, sp_dst / item.name)
        elif item.suffix in (".pth", ".dist-info"):
            continue
        else:
            shutil.copy2(item, sp_dst / item.name)

    # 4) 二进制依赖（ctypes/tk/opencv 都靠这些 dll）
    print("  复制 Library\\bin （DLL 依赖，较大）...")
    libbin = base / "Library" / "bin"
    dstbin = py / "Library" / "bin"
    dstbin.mkdir(parents=True, exist_ok=True)
    for f in libbin.glob("*.dll"):
        shutil.copy2(f, dstbin / f.name)
    for n in ("tcl86t.dll", "tk86t.dll"):
        src = libbin / n
        if src.exists():
            shutil.copy2(src, py / n)

    # 5) 应用本体
    print("  复制应用 ...")
    for item in ("boss_vision", "运营助理知识库_v0.1", "tools"):
        copy_tree(REPO / item, app / item)
    for f in ("requirements.txt", "全国普通高等学校名单.xls", "全国成人高等学校名单.xls"):
        src = REPO / f
        if src.exists():
            shutil.copy2(src, app / f)
    # 学校名单（判定硬规则依赖，缺了会把所有人判成"不在名单"）
    data_src = REPO / "data"
    if data_src.exists():
        (app / "data").mkdir(parents=True, exist_ok=True)
        for f in data_src.glob("schools*"):
            shutil.copy2(f, app / "data" / f.name)
    ex = REPO / "config.example.json"
    if ex.exists():
        shutil.copy2(ex, app / "config.json")      # 模板，不含真实密钥

    # 6) 启动脚本
    (out / "启动控制台.cmd").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        'cd /d "%~dp0"\r\n'
        'set "PATH=%~dp0python;%~dp0python\\Library\\bin;%~dp0python\\DLLs;%PATH%"\r\n'
        'start "BOSS Collector Console" cmd /k ""%~dp0python\\python.exe" -E "%~dp0app\\boss_vision\\gui.py""\r\n',
        encoding="ascii")
    edge = REPO / "run-edge-debug.cmd"
    if edge.exists():
        shutil.copy2(edge, out / "启动调试浏览器.cmd")
    (out / "使用说明.md").write_text(
        "# BOSS 招聘助手（免安装 Python）\n\n"
        "## 首次使用\n"
        "1. 编辑 `app\\config.json`，填两个 key：vision.api_key / judge.api_key\n"
        "2. 双击 `启动调试浏览器.cmd`（带调试端口的 Edge），在里面登录 BOSS\n"
        "3. 双击 `启动控制台.cmd`\n\n"
        "## 每次运行\n"
        "- 先 `启动调试浏览器.cmd`，再 `启动控制台.cmd`\n"
        "- GUI 里勾「后台模式」→ 不碰鼠标，窗口可被遮挡\n"
        "- 勾「刷新」→ 列表到底时自动点「刷新」换一批\n\n"
        "## 检查调试端口\n"
        "```\nTest-NetConnection 127.0.0.1 -Port 9222 -InformationLevel Quiet\n```\n"
        "必须 True，否则先用 `启动调试浏览器.cmd` 启动浏览器。\n\n"
        "## 结果\n"
        "`app\\boss_vision\\runs\\<时间>-summary.csv`（含「已打招呼」列）\n",
        encoding="utf-8")

    # 7) 自检
    print("  自检 ...")
    r = subprocess.run(
        [str(py / "python.exe"), "-E", "-c",
         "import ctypes, numpy, cv2, PIL, openpyxl, tkinter; print('ok')"],
        capture_output=True, text=True,
        env={"PATH": f"{py};{dstbin};{py / 'DLLs'};{sys.prefix}"})
    out_txt = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    print("   ", out_txt[-1][:160] if out_txt else "(无输出)")

    # 8) 压缩
    zpath = DIST / f"{name}.zip"
    if zpath.exists():
        zpath.unlink()
    print("  压缩中（约 100-500MB，需要几分钟）...")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in out.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(DIST))

    print(f"\n  目录: {out}")
    print(f"  压缩包: {zpath}  ({zpath.stat().st_size / 1024 / 1024:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
