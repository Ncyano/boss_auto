"""下载 macOS 免安装 Python 运行时（python-build-standalone install_only）。

产物：data/macpython/{arm64,x86_64}/python.tar.gz
安装脚本解压后即可用 ./python/bin/python3，无需系统 Python、无需管理员。

用法：
    python tools/fetch_mac_python.py            # 两个架构都下
    python tools/fetch_mac_python.py --arch arm64
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "data" / "macpython"
API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PY = "3.13"


def latest_assets() -> dict[str, str]:
    req = urllib.request.Request(API, headers={"User-Agent": "boss-auto"})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    out = {}
    for a in data.get("assets", []):
        name = a["name"]
        if not name.startswith(f"cpython-{PY}"):
            continue
        if "install_only.tar.gz" not in name:
            continue
        if "-freethreaded" in name:
            continue
        for arch in ("aarch64", "x86_64"):
            if f"{arch}-apple-darwin" in name:
                out[arch] = a["browser_download_url"]
    return out


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 5_000_000:
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "boss-auto"})
    with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="both", choices=["arm64", "x86_64", "both"])
    args = ap.parse_args()
    assets = latest_assets()
    print("可用:", {k: v.split("/")[-1] for k, v in assets.items()})

    targets = {"arm64": "aarch64", "x86_64": "x86_64"}
    picks = list(targets) if args.arch == "both" else [args.arch]
    for arch in picks:
        key = targets[arch]
        url = assets.get(key)
        if not url:
            print(f"  {arch}: 没有匹配的运行时")
            continue
        dest = DEST / arch / "python.tar.gz"
        try:
            p = download(url, dest)
            print(f"  {arch}: {p.name}  {p.stat().st_size/1024/1024:.1f} MB")
        except Exception as exc:
            print(f"  {arch}: 下载失败 {exc}")

    total = sum(f.stat().st_size for f in DEST.rglob("*.tar.gz"))
    print(f"\n合计 {total/1024/1024:.1f} MB -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
