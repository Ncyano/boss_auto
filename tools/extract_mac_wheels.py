"""把 macOS wheel 预解压成可直接用的依赖目录（免 pip、免 venv）。

产物：data/maclibs/{arm64,x86_64}/  —— 解压后的 site-packages 内容
运行时只需 PYTHONPATH 指向它即可 import numpy/cv2/PIL/openpyxl。
"""
from __future__ import annotations

import pathlib
import zipfile

ROOT = pathlib.Path(".").resolve()
WH = ROOT / "data" / "wheelhouse"
OUT = ROOT / "data" / "maclibs"


def main() -> int:
    for arch in ("arm64", "x86_64"):
        src = WH / arch
        dst = OUT / arch
        if not src.exists():
            print(f"  {arch}: 缺少 wheel 目录 {src}")
            continue
        dst.mkdir(parents=True, exist_ok=True)
        wheels = sorted(src.glob("*.whl"))
        n = 0
        for w in wheels:
            with zipfile.ZipFile(w) as z:
                for item in z.namelist():
                    # 跳过 wheel 元数据目录（安装后不需要）
                    if item.endswith("/") or ".dist-info/" in item or item.startswith("__pycache__"):
                        continue
                    z.extract(item, dst)
                    n += 1
            print(f"  {arch}: 解压 {w.name}")
        size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
        print(f"  {arch}: {n} 个文件，{size/1024/1024:.1f} MB -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
