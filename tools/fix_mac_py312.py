"""为老系统（macOS 11~12）构建兼容组合：
Python 3.12 + numpy 1.26.4 + opencv 4.9 + pillow(cp312) + openpyxl。

原因：Python 3.13 只能配 numpy>=2，而 opencv 4.9（macOS 11 唯一可用版本）
      是用 numpy 1.x 编译的，ABI 不兼容 → ImportError: numpy.core.multiarray。
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.request
import zipfile

ROOT = pathlib.Path(".").resolve()
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEST = ROOT / "data" / "wheelhouse312"
LIBS = ROOT / "data" / "maclibs312"
PYDIR = ROOT / "data" / "macpython312"
OUT = ROOT / "dist"

WANT = {
    "arm64": [
        "numpy-1.26.4-cp312-cp312-macosx_11_0_arm64.whl",
        "opencv_python-4.9.0.80-cp37-abi3-macosx_11_0_arm64.whl",
        "pillow-11.3.0-cp312-cp312-macosx_11_0_arm64.whl",
    ],
    "x86_64": [
        "numpy-1.26.4-cp312-cp312-macosx_10_9_x86_64.whl",
        "opencv_python-4.9.0.80-cp37-abi3-macosx_10_16_x86_64.whl",
        "pillow-11.3.0-cp312-cp312-macosx_10_13_x86_64.whl",
    ],
}
ANY = ["openpyxl-3.1.5-py2.py3-none-any.whl", "et_xmlfile-2.0.0-py3-none-any.whl"]
PKG_OF = {"numpy": "numpy", "opencv_python": "opencv-python",
          "pillow": "pillow", "openpyxl": "openpyxl", "et_xmlfile": "et-xmlfile"}


def index(pkg: str) -> str:
    req = urllib.request.Request(f"{MIRROR}/{pkg}/", headers={"User-Agent": "pip/24"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")


def find_link(pkg: str, filename: str) -> str | None:
    html = index(pkg)
    for line in html.splitlines():
        if filename in line:
            m = re.search(r'href="([^"]+?\.whl)[^"]*"', line)
            if m:
                return m.group(1)
    return None


def download(pkg: str, filename: str, dest: pathlib.Path) -> pathlib.Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 100_000:
        return dest
    link = find_link(pkg, filename)
    if not link:
        raise RuntimeError(f"镜像没找到 {filename}")
    url = link if link.startswith("http") else f"{MIRROR}/{pkg}/{link.lstrip('/')}"
    req = urllib.request.Request(url.split("#")[0], headers={"User-Agent": "pip/24"})
    with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


def fetch_python(arch: str) -> pathlib.Path:
    api = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    data = json.loads(urllib.request.urlopen(
        urllib.request.Request(api, headers={"User-Agent": "boss-auto"}), timeout=60
    ).read().decode())
    want_arch = "aarch64" if arch == "arm64" else "x86_64"
    for a in data.get("assets", []):
        n = a["name"]
        if (n.startswith("cpython-3.12") and "install_only.tar.gz" in n
                and f"{want_arch}-apple-darwin" in n and "freethreaded" not in n):
            dest = PYDIR / arch / "python.tar.gz"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size > 5_000_000:
                return dest
            req = urllib.request.Request(a["browser_download_url"],
                                         headers={"User-Agent": "boss-auto"})
            with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            return dest
    raise RuntimeError("没有找到 3.12 的运行时")


def main() -> int:
    patch = OUT / "mac-fix2-py312.zip"
    OUT.mkdir(exist_ok=True)
    with zipfile.ZipFile(patch, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as pz:
        for arch in ("arm64", "x86_64"):
            print(f"=== {arch} ===")
            py = fetch_python(arch)
            print(f"  Python 3.12: {py.stat().st_size/1024/1024:.1f} MB")
            pz.write(py, f"python/{arch}/python.tar.gz")

            libdir = LIBS / arch
            if libdir.exists():
                import shutil
                shutil.rmtree(libdir)
            libdir.mkdir(parents=True)
            files = list(WANT[arch]) + ANY
            for fn in files:
                pkg = PKG_OF[fn.split("-")[0].lower()]
                whl = download(pkg, fn, DEST / arch / fn)
                with zipfile.ZipFile(whl) as z:
                    for item in z.namelist():
                        if item.endswith("/") or ".dist-info/" in item:
                            continue
                        z.extract(item, libdir)
                print(f"  {fn}  ({whl.stat().st_size/1024/1024:.1f} MB)")
            for f in libdir.rglob("*"):
                if f.is_file():
                    pz.write(f, f"libs/{arch}/{f.relative_to(libdir).as_posix()}")
    print(f"\n补丁: {patch}  {patch.stat().st_size/1024/1024:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
