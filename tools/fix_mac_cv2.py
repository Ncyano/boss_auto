"""用 opencv 4.9（兼容 macOS 11+）替换 4.14，并生成一个只含 cv2 的补丁包。

背景：opencv 4.14 的 libavdevice 引用了 macOS 14.4+ 才有的
AVCaptureDeviceTypeDeskViewCamera 符号，老系统上 ImportError: dlopen。
"""
from __future__ import annotations

import pathlib
import shutil
import urllib.request
import zipfile

ROOT = pathlib.Path(".").resolve()
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEST = ROOT / "data" / "wheelhouse"
LIBS = ROOT / "data" / "maclibs"
OUT = ROOT / "dist"

TARGETS = {
    "arm64": "opencv_python-4.9.0.80-cp37-abi3-macosx_11_0_arm64.whl",
    "x86_64": "opencv_python-4.9.0.80-cp37-abi3-macosx_10_16_x86_64.whl",
}


def fetch_wheel(arch: str, filename: str) -> pathlib.Path:
    dest = DEST / arch / filename
    if dest.exists() and dest.stat().st_size > 10_000_000:
        return dest
    html = urllib.request.urlopen(
        urllib.request.Request(f"{MIRROR}/opencv-python/",
                               headers={"User-Agent": "pip/24"}), timeout=60
    ).read().decode("utf-8", "ignore")
    link = None
    for line in html.splitlines():
        if filename in line:
            import re
            m = re.search(r'href="([^"]+?\.whl)[^"]*"', line)
            if m:
                link = m.group(1)
                break
    if not link:
        raise RuntimeError(f"镜像里没找到 {filename}")
    url = link if link.startswith("http") else f"{MIRROR}/opencv-python/{link.lstrip('/')}"
    req = urllib.request.Request(url.split("#")[0], headers={"User-Agent": "pip/24"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


def main() -> int:
    patch = OUT / "cv2-4.9-fix-mac.zip"
    OUT.mkdir(exist_ok=True)
    with zipfile.ZipFile(patch, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as pz:
        for arch, whl_name in TARGETS.items():
            print(f"=== {arch} ===")
            whl = fetch_wheel(arch, whl_name)
            print(f"  wheel: {whl.name}  {whl.stat().st_size/1024/1024:.1f} MB")

            libdir = LIBS / arch
            cv2 = libdir / "cv2"
            if cv2.exists():
                shutil.rmtree(cv2)
            with zipfile.ZipFile(whl) as z:
                for item in z.namelist():
                    if not item.startswith("cv2/"):
                        continue
                    z.extract(item, libdir)
            n = sum(1 for _ in cv2.rglob("*"))
            print(f"  已替换 cv2：{n} 个文件")

            # 打进补丁包
            for f in cv2.rglob("*"):
                if f.is_file():
                    pz.write(f, f"libs/{arch}/cv2/{f.relative_to(cv2).as_posix()}")
    size = patch.stat().st_size / 1024 / 1024
    print(f"\n补丁: {patch}  {size:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
