"""从清华镜像直接抓 macOS wheel（不依赖 pip 的解析器，避免卡住）。

用法：
    python tools/fetch_mac_wheels.py            # 下载 arm64 + x86_64
    python tools/fetch_mac_wheels.py --arch arm64
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "wheelhouse"

PACKAGES = ["numpy", "opencv-python", "pillow", "openpyxl", "et-xmlfile"]

# 目标：CPython 3.13 + macOS（arm64 / x86_64），优先较新版本
ARCH_TAG = {"arm64": "arm64", "x86_64": "x86_64"}


def fetch_index(pkg: str) -> str:
    url = f"{MIRROR}/{pkg}/"
    req = urllib.request.Request(url, headers={"User-Agent": "pip/24"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def pick(html: str, arch: str) -> list[str]:
    links = re.findall(r'href="([^"]+?\.whl)[^"]*"', html)
    # 兼容两类命名：
    #   numpy/pillow: cp313-cp313-macosx_11_0_arm64.whl
    #   opencv      : cp37-abi3-macosx_11_0_arm64.whl（abi3，任何 3.13 可用）
    pat = re.compile(
        rf"(?:cp313-cp313|cp37-abi3|py3-none-any|cp313-abi3)"
        rf"[^/]*macosx_[\d_]+(?:{ARCH_TAG[arch]}|universal2)\.whl$"
        rf"|py2\.py3-none-any\.whl$"
        rf"|py3-none-any\.whl$"
    )
    out = []
    for ln in links:
        name = ln.split("/")[-1]
        if pat.search(name) or ("py3-none-any" in name and "openpyxl" in name):
            out.append(ln)
    # 版本排序：取最新，但 numpy 限制在 2.x（3.13 兼容）
    def ver_key(u: str):
        m = re.search(r"-(\d+)\.(\d+)\.(\d+)", u.split("/")[-1])
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    out.sort(key=ver_key, reverse=True)
    return out


def download(link: str, dest: Path) -> Path:
    if link.startswith("../"):
        url = "https://pypi.tuna.tsinghua.edu.cn/simple/" + link[3:]
    elif link.startswith("http"):
        url = link
    else:
        url = MIRROR + "/" + link.lstrip("/")
    url = url.split("#")[0]
    name = url.split("/")[-1]
    target = dest / name
    if target.exists():
        return target
    req = urllib.request.Request(url, headers={"User-Agent": "pip/24"})
    with urllib.request.urlopen(req, timeout=120) as r, target.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="both", choices=["arm64", "x86_64", "both"])
    args = ap.parse_args()
    archs = ["arm64", "x86_64"] if args.arch == "both" else [args.arch]

    for arch in archs:
        dest = WAREHOUSE / arch
        dest.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {arch} ===")
        for pkg in PACKAGES:
            try:
                html = fetch_index(pkg)
            except Exception as exc:
                print(f"  {pkg}: 索引失败 {exc}")
                continue
            cands = pick(html, arch)
            if not cands:
                print(f"  {pkg}: 没有匹配的 wheel")
                continue
            # 优先稳定版（排除 a/b/rc），opencv 优先 4.x，numpy 优先 2.x
            def is_stable(link: str) -> bool:
                name = link.split("/")[-1]
                return not re.search(r"\d(?:a|b|rc)\d", name)

            pool = [c for c in cands if is_stable(c)] or cands
            best = pool[0]
            want_major = {"numpy": "2", "opencv-python": "4"}.get(pkg)
            if want_major:
                for c in pool:
                    m = re.search(r"-(\d+)\.", c.split("/")[-1])
                    if m and m.group(1) == want_major:
                        best = c
                        break
            try:
                p = download(best, dest)
                print(f"  {pkg}: {p.name}  {p.stat().st_size/1024/1024:.1f} MB")
            except Exception as exc:
                print(f"  {pkg}: 下载失败 {exc}")
    total = sum(f.stat().st_size for f in WAREHOUSE.rglob("*.whl"))
    print(f"\n合计 {total/1024/1024:.1f} MB -> {WAREHOUSE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
