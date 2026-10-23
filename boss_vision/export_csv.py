"""把某次运行的 results.json 汇总成表格 / CSV。

工作经历按「第1家 / 第2家 / …」展开，最多 10 家，每家给 公司 / 职位 / 起止 / 工作内容。

    python export_csv.py                     # 用最新一次运行
    python export_csv.py --file runs/xxx.json
    python export_csv.py --list              # 列出所有运行
    python export_csv.py --all               # 所有批次合并成 runs\\all-summary.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
RUNS = _HERE / "runs"

MAX_COMPANIES = 10                       # 工作经历最多展开 10 家
WORK_FIELDS = ("公司", "职位", "起止", "工作内容")

HEAD_COLUMNS = ("批次", "序号", "姓名", "年龄", "学历性质", "毕业年份", "求职状态",
                "已打开", "工作经历条数")
TAIL_COLUMNS = ("教育条数", "学校", "专业", "学历", "教育起止", "滚屏数", "停止原因",
                "采集完成", "识别方式", "屏数", "读到章节", "冲突数", "缺失字段", "姓名核验", "姓名候选",
                "需复核", "复核原因", "判定结论", "判定依据", "判定环节", "间隔(秒)")

# 控制台只显示精简列（10 家全展开太宽，CSV 里是全量）
TABLE_COLUMNS = ("序号", "姓名", "已打开", "工作经历条数",
                 "第1家公司", "第1家职位", "学校", "学历", "停止原因", "间隔(秒)")
TABLE_LIMIT = 36                          # 控制台长字段截断长度


def work_columns() -> list[str]:
    cols: list[str] = []
    for i in range(1, MAX_COMPANIES + 1):
        cols.extend(f"第{i}家{f}" for f in WORK_FIELDS)
    return cols


def csv_columns() -> list[str]:
    return list(HEAD_COLUMNS) + work_columns() + list(TAIL_COLUMNS)


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def entry_text(entry: dict) -> str:
    """一条经历的「工作内容」：描述 + 标签。"""
    parts = [str(p).strip() for p in (entry.get("descriptions") or []) if str(p).strip()]
    tags = [str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()]
    if tags:
        parts.append("标签：" + "/".join(tags))
    return "；".join(parts)


def row_of(rec: dict, batch: str | None = None) -> dict[str, object]:
    """一条 results 记录 -> 一行表格（顺带套用 OCR 错字表）。"""
    try:
        import textfix

        rec = textfix.fix_obj(rec)
    except Exception:
        pass

    works = rec.get("work") or []
    edus = rec.get("education") or []
    top_edu: dict = edus[0] if edus else {}

    row: dict[str, object] = {}
    if batch is not None:
        row["批次"] = batch
    basic = rec.get("basic") or {}
    row.update({
        "序号": rec.get("index"),
        "姓名": rec.get("name", ""),
        "年龄": basic.get("age") or "",
        "学历性质": basic.get("education_mode", ""),
        "毕业年份": basic.get("graduation_year") or "",
        "求职状态": basic.get("status", ""),
        "已打开": "是" if rec.get("opened") else "否",
        "工作经历条数": len(works),
    })

    for i in range(MAX_COMPANIES):
        w = works[i] if i < len(works) else {}
        row[f"第{i + 1}家公司"] = w.get("company", "")
        row[f"第{i + 1}家职位"] = w.get("title", "")
        row[f"第{i + 1}家起止"] = w.get("period", "")
        row[f"第{i + 1}家工作内容"] = entry_text(w) if w else ""

    row.update({
        "教育条数": len(edus),
        "学校": top_edu.get("school", ""),
        "专业": top_edu.get("major", ""),
        "学历": top_edu.get("degree", ""),
        "教育起止": top_edu.get("period", ""),
        "滚屏数": rec.get("steps", ""),
        "停止原因": rec.get("stopped_reason", ""),
        "采集完成": "是" if rec.get("complete") or rec.get("reached_education") else "否",
        "识别方式": rec.get("reader", "ocr"),
        "屏数": rec.get("screen_count") or (int(rec.get("steps") or 0) + 1),
        "读到章节": "/".join(rec.get("section_seen") or []),
        "冲突数": len(rec.get("conflicts") or []),
        "缺失字段": "；".join(rec.get("missing") or []),
        "姓名核验": "；".join(rec.get("name_issues") or []),
        "姓名候选": "/".join(rec.get("name_candidates") or []),
        "需复核": "是" if rec.get("needs_review") else "否",
        "复核原因": rec.get("review_reason", ""),
        "判定结论": (rec.get("judgment") or {}).get("verdict", ""),
        "处理状态": (rec.get("judgment") or {}).get("status", ""),
        "判定依据": (rec.get("judgment") or {}).get("reason", ""),
        "判定环节": (rec.get("judgment") or {}).get("stage", ""),
        "间隔(秒)": rec.get("gap_before_next", ""),
    })
    return row


def write_csv(out: Path, rows: list[dict[str, object]]) -> Path | None:
    if not rows:
        return None
    cols = [c for c in csv_columns() if c in rows[0]]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV: {out}")
    return out


def print_table(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("没有数据")
        return

    def display(value: object) -> str:
        text = str(value).replace("\n", " / ")
        return text[:TABLE_LIMIT] + "…" if len(text) > TABLE_LIMIT else text

    cols = [c for c in TABLE_COLUMNS if c in rows[0]]
    cells = [[display(r.get(c, "")) for c in cols] for r in rows]
    widths = [max([len(c), *(len(row[i]) for row in cells)]) for i, c in enumerate(cols)]
    print(" | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("-+-".join("-" * widths[i] for i in range(len(cols))))
    for row in cells:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    print("（控制台只显示精简列；10 家公司的完整明细在 CSV 里）")


def list_runs(files: list[Path]) -> None:
    for path in files:
        data = load(path)
        opened = sum(1 for r in data if r.get("opened"))
        print(f"{path.name}  候选人数={len(data)}  成功打开={opened}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="指定某个 results.json")
    ap.add_argument("--list", action="store_true", help="列出所有批次")
    ap.add_argument("--all", action="store_true", help="合并所有批次到 runs\\all-summary.csv")
    ap.add_argument("--today", action="store_true",
                    help="把所有批次并进当天那张表 runs\\YYYY-MM-DD.csv")
    args = ap.parse_args()

    files = sorted(RUNS.glob("*-results.json"))
    if args.list:
        list_runs(files)
        return 0
    if not files:
        print("runs/ 里还没有 results.json")
        return 2

    if args.today:
        rows: list[dict[str, object]] = []
        for path in files:
            rows.extend(row_of(rec) for rec in load(path))
        merge_into_daily(RUNS, rows)
        print_table(rows)
        return 0

    if args.all:
        rows: list[dict[str, object]] = []
        for path in files:
            batch = path.name.replace("-results.json", "")
            rows.extend(row_of(rec, batch) for rec in load(path))
        out = write_csv(RUNS / "all-summary.csv", rows)
        print(f"合并 {len(files)} 个批次，共 {len(rows)} 条 -> {out}")
        print_table(rows)
        return 0

    path = Path(args.file) if args.file else files[-1]
    data = load(path)
    print(f"来源: {path.name}   候选人数: {len(data)}\n")
    rows = [row_of(rec) for rec in data]
    write_csv(path.with_name(path.stem.replace("-results", "") + "-summary.csv"), rows)
    print_table(rows)
    return 0


def daily_path(runs_dir: Path, day: str | None = None) -> Path:
    """按日期命名的汇总表：runs\\2026-10-21.csv"""
    day = day or time.strftime("%Y-%m-%d")
    return runs_dir / f"{day}.csv"


def dedupe_key(row: dict) -> tuple:
    """同一个人的指纹：姓名 + 学校 + 第一家公司 + 起止。

    只按姓名会在"同名不同人"时误删，所以带上学校与首段经历；
    四项都相同才认为是重复。
    """
    return (str(row.get("姓名", "")).strip(),
            str(row.get("学校", "")).strip(),
            str(row.get("第1家公司", "")).strip(),
            str(row.get("第1家起止", "")).replace(" ", ""))


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """同一个人只留一条（组合键相同才合并，同名不同人不合并）。

    组合键 = 姓名 + 学校 + 第一家公司 + 起止；姓名缺失的直接保留。
    """
    out: list[dict] = []
    index: dict[tuple, int] = {}
    for row in rows:
        name = str(row.get("姓名", "") or "").strip()
        if not name:
            out.append(row)
            continue
        key = dedupe_key(row)
        if key in index:
            out[index[key]] = row          # 后到的覆盖先到的
        else:
            index[key] = len(out)
            out.append(row)
    for i, row in enumerate(out, 1):
        row["序号"] = i
    return out


def merge_into_daily(runs_dir: Path, new_rows: list[dict], day: str | None = None) -> Path | None:
    """把本次采集的结果并进「当天那张表」，没有就新建。"""
    if not new_rows:
        return None
    path = daily_path(runs_dir, day)
    existing: list[dict] = []
    if path.exists():
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                existing = [dict(r) for r in csv.DictReader(fh)]
        except Exception as exc:
            print(f"[warn] 读取 {path.name} 失败，将重建：{exc}")
            existing = []

    # 组合键去重：姓名+学校+首家公司+起止，四项全同才算同一人（同名不同人不合并）
    merged: dict[tuple, dict] = {}
    anonymous: list[dict] = []
    for row in existing + new_rows:
        if not str(row.get("姓名", "")).strip():
            anonymous.append(row)          # 没姓名的不参与合并，保留
            continue
        merged[dedupe_key(row)] = row      # 后采的覆盖先采的

    ordered = list(merged.values()) + anonymous
    for i, row in enumerate(ordered, 1):
        row["序号"] = i

    write_csv(path, ordered)
    print(f"今日汇总表：{path}（累计 {len(ordered)} 条）")
    return path


if __name__ == "__main__":
    sys.exit(main())
