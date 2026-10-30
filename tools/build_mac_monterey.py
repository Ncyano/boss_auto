"""为 macOS 12 (Monterey / M2) 构建可用组合：
Python 3.12 + numpy 1.26.4 + opencv 4.9.0.80 + pillow(cp312) + openpyxl。

输出覆盖到：
    data/macpython/arm64/python.tar.gz     （3.12 运行时）
    data/maclibs/arm64/                    （预解压依赖）
这样 build_mac_source.py 与 启动-Mac.command 无需改动。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(".").resolve()
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYDEST = ROOT / "data" / "macpython" / "arm64" / "python.tar.gz"
LIBS = ROOT / "data" / "maclibs" / "arm64"

WHEELS = [
    ("numpy", "numpy-1.26.4-cp312-cp312-macosx_11_0_arm64.whl"),
    ("opencv-python", "opencv_python-4.9.0.80-cp37-abi3-macosx_11_0_arm64.whl"),
    ("pillow", "pillow-11.3.0-cp312-cp312-macosx_11_0_arm64.whl"),
    ("openpyxl", "openpyxl-3.1.5-py2.py3-none-any.whl"),
    ("et-xmlfile", "et_xmlfile-2.0.0-py3-none-any.whl"),
]


def get(url: str, timeout: int = 120, tries: int = 4) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "boss-auto"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            print(f"    重试 {i+1}/{tries}: {exc}")
            time.sleep(2)
    raise RuntimeError(f"下载失败: {last}")


def py312_url() -> str:
    api = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    data = json.loads(get(api, timeout=90).decode())
    for a in data.get("assets", []):
        n = a["name"]
        if (n.startswith("cpython-3.12") and "install_only.tar.gz" in n
                and "aarch64-apple-darwin" in n and "freethreaded" not in n):
            return a["browser_download_url"]
    raise RuntimeError("没找到 3.12 aarch64 运行时")


def fetch_python() -> None:
    if PYDEST.exists() and PYDEST.stat().st_size > 20_000_000:
        print(f"  Python 运行时已存在 {PYDEST.stat().st_size/1024/1024:.1f} MB")
        return
    url = py312_url()
    print(f"  下载 {url.split('/')[-1]}")
    blob = get(url, timeout=300)
    PYDEST.parent.mkdir(parents=True, exist_ok=True)
    PYDEST.write_bytes(blob)
    print(f"  已保存 {PYDEST.stat().st_size/1024/1024:.1f} MB")


def fetch_wheels() -> None:
    if LIBS.exists():
        shutil.rmtree(LIBS)
    LIBS.mkdir(parents=True)
    for pkg, fn in WHEELS:
        print(f"  {fn}")
        html = get(f"{MIRROR}/{pkg}/", timeout=90).decode("utf-8", "ignore")
        link = None
        for line in html.splitlines():
            if fn in line:
                m = re.search(r'href="([^"]+?\.whl)[^"]*"', line)
                if m:
                    link = m.group(1)
                    break
        if not link:
            raise RuntimeError(f"镜像没找到 {fn}")
        url = link if link.startswith("http") else f"{MIRROR}/{pkg}/{link.lstrip('/')}"
        blob = get(url.split("#")[0], timeout=300)
        tmp = LIBS / fn
        tmp.write_bytes(blob)
        with zipfile.ZipFile(tmp) as z:
            for item in z.namelist():
                if item.endswith("/") or ".dist-info/" in item:
                    continue
                z.extract(item, LIBS)
        tmp.unlink()
        print(f"    {len(blob)/1024/1024:.1f} MB 已解压")


def main() -> int:
    print("=== Python 3.12 运行时 ===")
    fetch_python()
    print("=== cp312 依赖 ===")
    fetch_wheels()
    size = sum(f.stat().st_size for f in LIBS.rglob("*") if f.is_file())
    print(f"\nlibs/arm64: {size/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
