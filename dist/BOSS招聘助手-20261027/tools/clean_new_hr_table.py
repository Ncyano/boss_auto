"""清洗新人事表格，抽出「运营助理」岗位数据。

输入：未命名表格 (1).xlsx（4 个招聘人工作表，每行一条候选人记录 + 内嵌简历截图）
输出：
  data/新人事-运营助理.csv        清洗后的结构化数据
  data/新人事截图/<招聘人>/        按行导出的简历截图（可选）

用法：
  python tools\\clean_new_hr_table.py
  python tools\\clean_new_hr_table.py --no-images      # 只出 CSV，不导图片
  python tools\\clean_new_hr_table.py --job 运营助理    # 换岗位关键词
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

XLSX = REPO / "未命名表格 (1).xlsx"
OUT_CSV = REPO / "data" / "新人事-运营助理.csv"
OUT_IMG_DIR = REPO / "data" / "新人事截图"

HEADER_HINT = ("序号", "时间", "招聘岗位")

COLUMNS = [
    "序号", "招聘人", "时间", "招聘岗位", "判定",
    "不合适原因", "合适原因", "原因原文", "截图数", "行号",
]


def clean_text(value) -> str:
    """统一空白、去掉序号前缀和多余分隔符。"""
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def find_header(ws) -> tuple[int, dict[str, int]]:
    """找表头行，返回 (行号, 列名->列索引)。"""
    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), 1):
        cells = [clean_text(c) for c in row]
        if all(any(h == c or c.startswith(h) for c in cells) for h in HEADER_HINT):
            mapping = {}
            for i, c in enumerate(cells, 1):
                if not c:
                    continue
                key = c.replace(" ", "")
                mapping[key] = i
                if key.startswith("在线简历"):
                    mapping.setdefault("截图层级", i)
            return row_idx, mapping
    return 1, {}


def column_of(mapping: dict[str, int], *names: str) -> int:
    for name in names:
        for key, idx in mapping.items():
            if key.startswith(name):
                return idx
    return 0


def images_by_row(ws) -> dict[int, list]:
    """把内嵌图片按锚点行号归组。"""
    out: dict[int, list] = {}
    for img in getattr(ws, "_images", []) or []:
        try:
            anchor = img.anchor
            row = None
            if hasattr(anchor, "_from"):
                row = anchor._from.row + 1
            elif isinstance(anchor, str):
                m = re.search(r"(\d+)$", anchor)
                row = int(m.group(1)) if m else None
            if row:
                out.setdefault(row, []).append(img)
        except Exception:
            continue
    return out


def save_image(img, path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = img._data() if hasattr(img, "_data") else None
        if data is None:
            return False
        ext = (getattr(img, "format", "") or "jpeg").lower()
        ext = {"jpg": "jpeg"}.get(ext, ext)
        path = path.with_suffix(".jpg" if ext in ("jpeg", "jpg") else f".{ext}")
        path.write_bytes(data)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default="运营助理", help="岗位关键词（默认 运营助理）")
    ap.add_argument("--no-images", action="store_true", help="不导出截图")
    ap.add_argument("--xlsx", default=str(XLSX))
    ap.add_argument("--tag", default="", help="输出去重后缀，如 2 -> 新人事-运营助理-2.csv")
    args = ap.parse_args()

    global OUT_CSV, OUT_IMG_DIR
    if args.tag:
        OUT_CSV = OUT_CSV.with_name(f"新人事-运营助理-{args.tag}.csv")
        OUT_IMG_DIR = OUT_IMG_DIR.with_name(f"新人事截图-{args.tag}")
    xlsx = Path(args.xlsx)
    if not xlsx.exists():
        raise SystemExit(f"找不到 {xlsx}")

    import openpyxl

    wb = openpyxl.load_workbook(xlsx, data_only=True)
    rows_out: list[dict] = []
    stats: dict[str, dict] = {}

    for ws in wb.worksheets:
        header_row, mapping = find_header(ws)
        c_seq = column_of(mapping, "序号")
        c_time = column_of(mapping, "时间")
        c_job = column_of(mapping, "招聘岗位")
        c_bad = column_of(mapping, "不合适")
        c_good = column_of(mapping, "合适")
        c_who = column_of(mapping, "招聘人")
        imgs = images_by_row(ws)

        total = kept = 0
        for row_idx, row in enumerate(
                ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row, values_only=True),
                header_row + 1):
            total += 1
            job = clean_text(row[c_job - 1]) if c_job and c_job <= len(row) else ""
            if args.job not in job:
                continue
            seq = clean_text(row[c_seq - 1]) if c_seq and c_seq <= len(row) else ""
            when = clean_text(row[c_time - 1]) if c_time and c_time <= len(row) else ""
            bad = clean_text(row[c_bad - 1]) if c_bad and c_bad <= len(row) else ""
            good = clean_text(row[c_good - 1]) if c_good and c_good <= len(row) else ""
            who = clean_text(row[c_who - 1]) if c_who and c_who <= len(row) else ws.title

            if good and not bad:
                verdict = "合适"
            elif bad and not good:
                verdict = "不合适"
            elif good and bad:
                verdict = "冲突"
            else:
                verdict = "未标注"

            pic_list = imgs.get(row_idx) or []
            saved = 0
            if pic_list and not args.no_images:
                failed = 0
                for k, img in enumerate(pic_list, 1):
                    # 用「招聘人-序号-行号-序号」保证唯一（不同招聘人表里序号会重复）
                    name = f"{ws.title}-{seq or row_idx}-r{row_idx}-{k}"
                    if save_image(img, OUT_IMG_DIR / ws.title / name):
                        saved += 1
                    else:
                        failed += 1
                if failed:
                    print(f"  [warn] {ws.title} #{seq}: {failed} 张截图导出失败")

            rows_out.append({
                "序号": seq or str(row_idx - header_row),
                "招聘人": who or ws.title,
                "时间": when,
                "招聘岗位": job,
                "判定": verdict,
                "不合适原因": bad,
                "合适原因": good,
                "原因原文": bad or good,
                "截图数": saved,
                "行号": row_idx,
                "_sheet": ws.title,
            })
            kept += 1

        stats[ws.title] = {"总行数": total, "运营助理": kept}

    wb.close()

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_out)

    # ---- 统计 ----
    from collections import Counter

    print(f"清洗完成：{OUT_CSV}（{len(rows_out)} 条）")
    print()
    print("各招聘人：")
    for title, st in stats.items():
        print(f"  {title:<8} 总 {st['总行数']:>3} 行 -> 运营助理 {st['运营助理']:>3} 条")
    print()
    print("判定分布：", dict(Counter(r["判定"] for r in rows_out)))
    print("招聘人分布：", dict(Counter(r["招聘人"] for r in rows_out)))
    print(f"带截图：{sum(1 for r in rows_out if r['截图数'])} 条")
    if not args.no_images:
        print(f"截图目录：{OUT_IMG_DIR}")
    print()
    print("原因关键词（前 8）：")
    words = Counter()
    for r in rows_out:
        for kw in ("全日制", "同行", "空档", "沟通", "客服", "销售", "年龄", "学历",
                   "稳定", "简历不完整", "应届"):
            if kw in r["原因原文"]:
                words[kw] += 1
    for kw, n in words.most_common(8):
        print(f"  {kw}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
