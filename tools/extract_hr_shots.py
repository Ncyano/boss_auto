"""把人事表格里内嵌的简历截图抽出来，按 (工作表, 行号) 归组。

    python tools/extract_hr_shots.py            # 只抽取 + 打印映射
    python tools/extract_hr_shots.py --dump     # 顺便把图片写到 runs/hr-shots/

产出：
    data/hr-shot-map.json   {工作表: {行号: [图片相对路径...]}}
    runs/hr-shots/<工作表>/r<行号>-<n>.png
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "未命名表格.xlsx"
OUT_DIR = ROOT / "boss_vision" / "runs" / "hr-shots"
MAP_PATH = ROOT / "data" / "hr-shot-map.json"


def sheet_to_drawing(z: zipfile.ZipFile) -> dict[str, str]:
    """工作表名 -> drawing 文件。"""
    wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8", "replace")
    rid_to_target = dict(re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rels))

    result: dict[str, str] = {}
    for m in re.finditer(r'<sheet[^>]*name="([^"]+)"[^>]*r:id="(rId\d+)"', wb):
        name, rid = m.group(1), m.group(2)
        target = rid_to_target.get(rid, "")
        if not target:
            continue
        sheet_file = "xl/" + target.lstrip("/")
        rels_path = posixpath.join(posixpath.dirname(sheet_file), "_rels",
                                   posixpath.basename(sheet_file) + ".rels")
        if rels_path not in z.namelist():
            continue
        rel = z.read(rels_path).decode("utf-8", "replace")
        base_dir = posixpath.dirname(sheet_file)          # 例如 xl/worksheets
        for rm in re.finditer(r'Id="(rId\d+)"[^>]*Target="([^"]*drawing[^"]*)"', rel):
            target = rm.group(2)
            if target.startswith("/"):
                resolved = target.lstrip("/")             # 绝对路径 /xl/drawings/x.xml
            else:
                resolved = posixpath.join(base_dir, target)   # 相对本 sheet 的目录
            result[name] = posixpath.normpath(resolved)
    return result


def drawing_images(z: zipfile.ZipFile, drawing: str) -> list[tuple[int, str]]:
    """drawing 文件 -> [(Excel 行号(1 起), 图片路径)]"""
    rels_path = posixpath.join(posixpath.dirname(drawing), "_rels",
                               posixpath.basename(drawing) + ".rels")
    rid_to_media: dict[str, str] = {}
    if rels_path in z.namelist():
        rel = z.read(rels_path).decode("utf-8", "replace")
        for rm in re.finditer(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rel):
            rid_to_media[rm.group(1)] = posixpath.normpath(
                posixpath.join(posixpath.dirname(drawing), rm.group(2)))

    xml = z.read(drawing).decode("utf-8", "replace")
    out: list[tuple[int, str]] = []
    for block in re.findall(r"<xdr:twoCellAnchor.*?</xdr:twoCellAnchor>", xml, re.S):
        row_m = re.search(r"<xdr:from>.*?<xdr:row>(\d+)</xdr:row>", block, re.S)
        rid_m = re.search(r'r:embed="(rId\d+)"', block)
        if not (row_m and rid_m):
            continue
        media = rid_to_media.get(rid_m.group(1))
        if media and media in z.namelist():
            out.append((int(row_m.group(1)) + 1, media))   # xml 里 0 起
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="把图片写出来")
    args = ap.parse_args(argv)

    z = zipfile.ZipFile(XLSX)
    mapping = sheet_to_drawing(z)
    print("工作表 -> drawing:", mapping)

    result: dict[str, dict[str, list[str]]] = {}
    total = 0
    for sheet, drawing in mapping.items():
        images = drawing_images(z, drawing)
        row_map: dict[str, list[str]] = {}
        for idx, (row, media) in enumerate(sorted(images), 1):
            row_map.setdefault(str(row), []).append(media)
        result[sheet] = row_map
        total += len(images)
        print(f"  {sheet}: {len(images)} 张，{len(row_map)} 行有图")

        if args.dump:
            folder = OUT_DIR / sheet
            folder.mkdir(parents=True, exist_ok=True)
            for row, medias in row_map.items():
                for n, media in enumerate(medias, 1):
                    data = z.read(media)
                    if data[:8] == b"\x89PNG\r\n\x1a\n":
                        suffix = ".png"
                    elif data[:2] == b"\xff\xd8":
                        suffix = ".jpg"
                    elif data[:4] == b"RIFF":
                        suffix = ".webp"
                    else:
                        suffix = Path(media).suffix or ".png"
                    (folder / f"r{row}-{n}{suffix}").write_bytes(data)

    MAP_PATH.parent.mkdir(exist_ok=True)
    MAP_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {total} 张图片；映射写入 {MAP_PATH}")
    if args.dump:
        print(f"图片已导出到 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
