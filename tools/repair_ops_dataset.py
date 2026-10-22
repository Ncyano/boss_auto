"""补采：把运营助理数据集里"读取失败"和"字段缺失"的记录重读一遍（放大 2 倍）。

    python tools\\repair_ops_dataset.py            # 补采并原地更新 CSV
    python tools\\repair_ops_dataset.py --dry-run  # 只看会补哪些，不写文件

做三件事：
    1. 人事表里有、但数据集里缺的记录 -> 整条重读补进来
    2. 有缺失字段的记录 -> 放大重读，补齐公司名/职责/姓名/学校/学历
    3. 重新计算完整性与统计报告
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import completeness  # noqa: E402
import vlm  # noqa: E402
from PIL import Image  # noqa: E402
import numpy as np  # noqa: E402

DATA = ROOT / "data"
SHOTS = ROOT / "boss_vision" / "runs" / "hr-shots"
CSV_PATH = DATA / "运营助理-数据集.csv"
REPORT = DATA / "运营助理-数据集报告.txt"
TODAY = dt.date.today()

PERIOD_RE = re.compile(r"(\d{4})[.\-/年]?(\d{1,2})?")
DEGREE_WORDS = ("博士", "硕士", "研究生", "本科", "大专", "专科", "中专", "中技", "高中", "初中")


def parse_period(text: str):
    s = (text or "").replace(" ", "")
    if not s:
        return None, None, False
    parts = PERIOD_RE.findall(s)
    current = ("至今" in s) or ("现在" in s)
    dates = []
    for year, month in parts:
        if not year:
            continue
        m = int(month) if month else 1
        m = m if 1 <= m <= 12 else 1
        try:
            dates.append(dt.date(int(year), m, 1))
        except ValueError:
            pass
    if not dates:
        return None, None, current
    return dates[0], (dates[-1] if len(dates) >= 2 else None), current


def months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def total_months(work: list[dict]) -> int:
    spans = []
    for w in work:
        start, end, current = parse_period(w.get("period", ""))
        if not start:
            continue
        stop = TODAY if current else (end or start)
        if stop < start:
            stop = start
        spans.append((start.year * 12 + start.month, stop.year * 12 + stop.month))
    if not spans:
        return 0
    spans.sort()
    merged = [list(spans[0])]
    for s0, e0 in spans[1:]:
        if s0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e0)
        else:
            merged.append([s0, e0])
    return sum(e0 - s0 for s0, e0 in merged)


def gap_months(work: list[dict]):
    latest = None
    for w in work:
        _s, end, current = parse_period(w.get("period", ""))
        if current:
            return 0
        if end:
            value = max(0, months_between(end, TODAY))
            latest = value if latest is None else min(latest, value)
    return latest


def norm_mode(text: str) -> str:
    t = (text or "").strip()
    if "非全日制" in t:
        return "非全日制"
    if "全日制" in t:
        return "全日制"
    return "未知"


def norm_degree(text: str) -> str:
    t = (text or "").strip()
    for word in DEGREE_WORDS:
        if word in t:
            return "大专" if word == "专科" else word
    return t


def read_images(images: list[Path], cfg: dict) -> dict:
    work, education, name, basic = [], [], "", {}
    for path in images:
        try:
            bgr = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()
        except Exception:
            continue
        try:
            raw = vlm.read_screen(bgr, cfg, log=lambda *_: None)
        except Exception as exc:
            print(f"      (单张失败：{str(exc)[:50]})")
            continue
        if not name:
            name = vlm.clean_name(raw.get("name") or "")
        if not basic:
            basic = vlm.normalize_basic(raw)
        mode = vlm.scan_education_mode(bgr)
        if mode:
            basic["education_mode"] = mode
        parts = vlm.normalize(raw["sections"])
        vlm.merge_screens(work, parts["work"])
        for e in parts["education"]:
            same = next((o for o in education
                         if not e.get("period") or not o.get("period")
                         or e["period"] == o["period"]), None)
            if same is None:
                education.append(e)
            else:
                for field in ("school", "major", "degree", "period"):
                    if not (same.get(field) or "").strip() and (e.get(field) or "").strip():
                        same[field] = e[field]
    return {"name": name, "basic": basic, "work": work, "education": education}


def build_row(base: dict, res: dict, images: list[Path], label: dict) -> dict:
    work = sorted(res["work"], key=lambda w: (
        1 if "至今" in (w.get("period") or "") else 0,
        (parse_period(w.get("period", ""))[1] or dt.date(1900, 1, 1)).toordinal()), reverse=True)
    education = res["education"]
    first = education[0] if education else {}
    ok, missing, nice = completeness.check({
        "name": res["name"], "basic": res["basic"], "education": education,
        "work": work, "collection": {"complete": bool(education)}})

    row = dict(base)
    row.update({
        "姓名": res["name"] or base.get("姓名", ""),
        "年龄": res["basic"].get("age") or base.get("年龄", ""),
        "学历性质": norm_mode(res["basic"].get("education_mode")) if res["basic"].get("education_mode")
                    else base.get("学历性质", "未知"),
        "毕业年份": res["basic"].get("graduation_year") or base.get("毕业年份", ""),
        "求职状态": res["basic"].get("status") or base.get("求职状态", ""),
        "学校": (first.get("school") or "").strip() or base.get("学校", ""),
        "专业": (first.get("major") or "").strip() or base.get("专业", ""),
        "学历": norm_degree(first.get("degree") or "") or base.get("学历", ""),
        "教育起止": (first.get("period") or "").strip() or base.get("教育起止", ""),
        "教育条数": len(education) or base.get("教育条数", ""),
        "工作条数": len(work) or base.get("工作条数", ""),
        "累计工作月数": total_months(work) or base.get("累计工作月数", ""),
        "最近空档月数": (gap_months(work) if gap_months(work) is not None
                      else base.get("最近空档月数", "")),
        "工作经历全文": "\n".join(
            f'{w.get("period", "")} {w.get("company", "")}/{w.get("title", "")}'
            + ("：" + "；".join(w.get("descriptions") or []) if w.get("descriptions") else "")
            for w in work) or base.get("工作经历全文", ""),
        "缺失字段": "；".join(missing),
        "建议补充": "、".join(nice),
        "采集完整": "是" if ok else "否",
        "截图张数": len(images) or base.get("截图张数", ""),
    })
    for i in range(1, 11):
        w = work[i - 1] if i - 1 < len(work) else {}
        row[f"第{i}家公司"] = w.get("company", "") or base.get(f"第{i}家公司", "")
        row[f"第{i}家职位"] = w.get("title", "") or base.get(f"第{i}家职位", "")
        row[f"第{i}家起止"] = w.get("period", "") or base.get(f"第{i}家起止", "")
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-missing", action="store_true",
                    help="只补读完全缺失的记录，不重读缺字段的（后者截图里往往就没有）")
    args = ap.parse_args(argv)

    cfg = vlm.load_config()
    if not vlm.api_key(cfg):
        print("没有视觉模型 key")
        return 2
    zoom = dict(cfg)
    zoom["max_image_side"] = int(cfg.get("max_image_scale", cfg.get("max_image_side", 1024))) * 2
    zoom["retries"] = 4

    labels = { (r["招聘人"] or r["工作表"], str(r["序号"]).strip()): r
               for r in csv.DictReader(open(DATA / "hr-labels.csv", encoding="utf-8-sig"))
               if "运营助理" in (r.get("岗位") or "") }
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
    columns = list(rows[0].keys())
    have = {(r["招聘人"], str(r["表内序号"]).strip()) for r in rows}

    missing_records = [k for k in labels if k not in have]
    incomplete = [r for r in rows if (r.get("缺失字段") or "").strip()]

    print(f"数据集 {len(rows)} 条；缺记录 {len(missing_records)}，缺字段 {len(incomplete)}\n")

    changed = 0

    for key in missing_records:
        label = labels[key]
        person, idx = key
        images = sorted((SHOTS / person).glob(f"r{int(idx) + 1}-*"))
        if not images:
            print(f"  跳过 {person}#{idx}：没有截图")
            continue
        print(f"  补读 {person}#{idx}（{len(images)} 张，放大 2 倍）")
        res = read_images(images, zoom)
        if not res["work"] and not res["education"]:
            print("     仍然读不出来，跳过")
            continue
        base = {c: "" for c in columns}
        base.update({"序号": str(len(rows) + 1), "招聘人": person, "表内序号": idx,
                     "人事结论": label.get("结论") or "",
                     "人事原因": (label.get("不合适原因") or label.get("合适原因") or ""
                               ).replace("；", " / ")})
        row = build_row(base, res, images, label)
        rows.append(row)
        changed += 1
        print(f"     补回：{row['姓名'] or '(无名)'} 工作{row['工作条数']} "
              f"教育{row['教育条数']} 缺失[{row['缺失字段'] or '无'}]")

    for row in ([] if args.only_missing else incomplete):
        person = row["招聘人"]
        idx = str(row["表内序号"]).strip()
        images = sorted((SHOTS / person).glob(f"r{int(idx) + 1}-*"))
        if not images:
            continue
        print(f"  放大重读 {row['姓名'] or '(无名)'}（{person}#{idx}，{len(images)} 张）")
        res = read_images(images, zoom)
        if not res["work"] and not res["education"]:
            print("     仍然读不出来")
            continue
        new_row = build_row(row, res, images, {})
        before = row.get("缺失字段") or ""
        after = new_row.get("缺失字段") or ""
        if len(after) < len(before) or (not after and before):
            row.update(new_row)
            changed += 1
            print(f"     缺失 [{before}] -> [{after or '无'}]")
        else:
            print(f"     没有改善（仍为 [{after}]）")

    print(f"\n共补采 {changed} 条")
    if args.dry_run:
        print("--dry-run：不写文件")
        return 0

    for i, row in enumerate(rows, 1):
        row["序号"] = str(i)
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    complete = sum(1 for r in rows if r.get("采集完整") == "是")
    verdict: dict[str, int] = {}
    for r in rows:
        verdict[r.get("人事结论") or ""] = verdict.get(r.get("人事结论") or "", 0) + 1
    miss: dict[str, int] = {}
    for r in rows:
        for m in (r.get("缺失字段") or "").split("；"):
            if m:
                miss[m] = miss.get(m, 0) + 1

    lines = [
        "运营助理 数据集清洗报告",
        "=" * 46,
        f"来源：{len(labels)} 条运营助理记录（人事表格）",
        f"数据集：{len(rows)} 条",
        f"采集完整：{complete}/{len(rows)}（{complete / len(rows):.0%}）",
        f"人事结论：{verdict}",
        "",
        "缺失字段统计：",
    ]
    for k, v in sorted(miss.items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"  {k}: {v}")
    if not miss:
        lines.append("  无")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\n-> {CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
