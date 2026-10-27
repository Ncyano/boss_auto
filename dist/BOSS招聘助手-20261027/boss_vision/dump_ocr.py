"""一次性导出 fixture 的 OCR 行，供免 OCR 的解析测试使用（跑一次约 40 秒）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import extract
import vision

FIX = _HERE / "tests" / "fixtures"


def main() -> int:
    engine = vision.load_ocr()
    if engine is None:
        print("OCR 未安装")
        return 2
    for png in sorted(FIX.glob("ex*.png")) + [FIX / "resume.png"]:
        if not png.exists():
            continue
        lines, wide = extract.lines_from_image(str(png), engine)
        out = {
            "source": png.name,
            "wide": None if wide is None else {"x": wide.x, "y": wide.y, "w": wide.w, "h": wide.h},
            "lines": lines,
        }
        dest = FIX / f"ocr_{png.stem}.json"
        dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"{png.name}: {len(lines)} 行 -> {dest.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
