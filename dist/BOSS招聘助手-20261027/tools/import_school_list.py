"""把教育部《全国普通高等学校名单》XLS 转成本地可查询的规范文件。

用法：
    python tools\\import_school_list.py                       # 用默认文件
    python tools\\import_school_list.py --file 某名单.xls

产出（都在 data\\ 下）：
    schools-normalized.csv   学校名称, 所在省份, 主管部门, 办学层次, 学校标识码, 备注
    schools.meta.json        来源、日期、SHA256、条数（便于追溯版本）

再配合 boss_vision\\schools.py 做离线查询，不需要联网。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_XLS = ROOT / "全国普通高等学校名单.xls"
KINDS = {
    "general": {
        "xls": ROOT / "全国普通高等学校名单.xls",
        "csv": "schools-normalized.csv",
        "meta": "schools.meta.json",
        "source_file": "http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/202606/W020260618416094865984.xls",
    },
    "adult": {
        "xls": ROOT / "全国成人高等学校名单.xls",
        "csv": "schools-adult.csv",
        "meta": "schools-adult.meta.json",
        "source_file": "http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/202606/W020260618307096079727.xls",
    },
}

# 名单来自教育部「全国高等学校名单」发布页（截至 2026-06-17）
SOURCE_PAGE = "http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/202606/t20260618_1441074.html"
SOURCE_FILE = "http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/202606/W020260618416094865984.xls"
LIST_DATE = "2026-06-17"

PROVINCE_RE = re.compile(r"^(.+?)（(\d+)所）$")
COLUMNS = ("学校名称", "所在省份", "主管部门", "办学层次", "学校标识码", "备注")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse(xls: Path) -> list[dict]:
    import xlrd

    book = xlrd.open_workbook(str(xls))
    sheet = book.sheet_by_index(0)

    rows: list[dict] = []
    province = ""
    for r in range(sheet.nrows):
        cells = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
        first = cells[0]
        name = cells[1] if len(cells) > 1 else ""

        if not name:
            m = PROVINCE_RE.match(first)
            if m:
                province = m.group(1)          # 「北京市（92所）」这类分隔行
            continue
        if name in ("学校名称",):               # 表头
            continue
        rows.append({
            "学校名称": name,
            "所在省份": province,
            "主管部门": cells[3] if len(cells) > 3 else "",
            "办学层次": cells[5] if len(cells) > 5 else "",
            "学校标识码": cells[2] if len(cells) > 2 else "",
            "备注": cells[6] if len(cells) > 6 else "",
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    ap.add_argument("--kind", choices=tuple(KINDS), default="general",
                    help="general=普通高校（默认），adult=成人高校")
    args = ap.parse_args()

    kind = KINDS[args.kind]
    xls = Path(args.file) if args.file else kind["xls"]
    if not xls.exists():
        print(f"找不到名单文件：{xls}")
        return 2

    rows = parse(xls)
    DATA.mkdir(exist_ok=True)
    out_csv = DATA / kind["csv"]
    with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    levels: dict[str, int] = {}
    for row in rows:
        levels[row["办学层次"]] = levels.get(row["办学层次"], 0) + 1

    meta = {
        "source_page": SOURCE_PAGE,
        "source_file": kind["source_file"],
        "list_date": LIST_DATE,
        "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_sha256": sha256_of(xls),
        "source_bytes": xls.stat().st_size,
        "record_count": len(rows),
        "by_level": levels,
        "province_count": len({r["所在省份"] for r in rows if r["所在省份"]}),
    }
    (DATA / kind["meta"]).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[{args.kind}] 已导入 {len(rows)} 所学校 -> {out_csv}")
    for level, n in sorted(levels.items(), key=lambda kv: -kv[1]):
        print(f"  {level or '(空)'}: {n}")
    print(f"省份数: {meta['province_count']}   SHA256: {meta['source_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
