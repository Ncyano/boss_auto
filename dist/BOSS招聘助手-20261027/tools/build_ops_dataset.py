"""只提取「运营助理」岗位，从人事表格的截图重新读一遍并清洗。

    python tools\\build_ops_dataset.py --limit 3      # 试水
    python tools\\build_ops_dataset.py --workers 4    # 全量 53 条

输入：
    data\\hr-labels.csv        人事判定（按岗位筛选）
    data\\hr-shot-map.json     工作表 -> 行号 -> 截图
    boss_vision\\runs\\hr-shots\\<工作表>\\r<行>-*.png

输出：
    data\\运营助理-数据集.csv    清洗后的结构化数据（含缺失字段、人事结论）
    data\\运营助理-数据集报告.txt  统计
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import completeness  # noqa: E402
import screening  # noqa: E402
import vlm  # noqa: E402
from PIL import Image  # noqa: E402
import numpy as np  # noqa: E402

DATA = ROOT / "data"
SHOTS = ROOT / "boss_vision" / "runs" / "hr-shots"
OUT = DATA / "运营助理-数据集.csv"
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
        start, end, current = parse_period(w.get("period", ""))
        if current:
            return 0
        if end:
            value = max(0, months_between(end, TODAY))
            latest = value if latest is None else min(latest, value)
    return latest


def read_one(images: list[Path], cfg: dict) -> dict:
    work, education = [], []
    name, basic = "", {}
    tokens = 0
    mode_forced = ""

    for path in images:
        try:
            bgr = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()
        except Exception:
            continue
        try:
            raw = vlm.read_screen(bgr, cfg, log=lambda *_: None)
        except Exception as exc:
            return {"error": str(exc)}
        tokens += int((raw.get("usage") or {}).get("total_tokens") or 0)

        if not name:
            name = vlm.clean_name(raw.get("name") or "")
        if not basic:
            basic = vlm.normalize_basic(raw)
        if not mode_forced:
            mode_forced = vlm.scan_education_mode(bgr)

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

    if mode_forced:
        basic["education_mode"] = mode_forced
        basic["education_mode_source"] = "ocr"

    return {"name": name, "basic": basic, "work": work, "education": education,
            "tokens": tokens, "screens": len(images)}


def norm_degree(text: str) -> str:
    t = (text or "").strip()
    for word in DEGREE_WORDS:
        if word in t:
            return "大专" if word == "专科" else word
    return t


def norm_mode(text: str) -> str:
    t = (text or "").strip()
    if "非全日制" in t:
        return "非全日制"
    if "全日制" in t:
        return "全日制"
    return "未知"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    cfg = vlm.load_config()
    if not vlm.api_key(cfg):
        print("没有视觉模型 key（config.json 的 vision.api_key）")
        return 2

    labels = [r for r in csv.DictReader(open(DATA / "hr-labels.csv", encoding="utf-8-sig"))
              if "运营助理" in (r.get("岗位") or "")]
    shot_map = json.loads((DATA / "hr-shot-map.json").read_text(encoding="utf-8"))
    print(f"运营助理记录 {len(labels)} 条")

    jobs = []
    for r in labels:
        folder = SHOTS / r["工作表"]
        images = sorted(folder.glob(f"r{int(r['序号']) + 1}-*"))
        if images:
            jobs.append((r, images))
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"待读取 {len(jobs)} 条，共 {sum(len(j[1]) for j in jobs)} 张截图\n")

    def run(job):
        r, images = job
        return r, images, read_one(images, cfg)

    rows, failed = [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for r, images, res in pool.map(run, jobs):
            label = r.get("结论") or ""
            if res.get("error"):
                failed += 1
                print(f"  ✗ {r['工作表']}#{r['序号']} 读取失败：{res['error'][:60]}")
                continue

            work = sorted(res["work"], key=lambda w: (
                1 if "至今" in (w.get("period") or "") else 0,
                (parse_period(w.get("period", ""))[1] or dt.date(1900, 1, 1)).toordinal()),
                reverse=True)
            education = res["education"]
            first = education[0] if education else {}

            resume = {"name": res["name"], "basic": res["basic"], "education": education,
                      "work": work, "collection": {"complete": bool(education)}}
            ok, missing, nice = completeness.check(resume)

            row = {
                "序号": len(rows) + 1,
                "招聘人": r.get("招聘人") or r["工作表"],
                "表内序号": r["序号"],
                "姓名": res["name"],
                "年龄": res["basic"].get("age") or "",
                "学历性质": norm_mode(res["basic"].get("education_mode")),
                "毕业年份": res["basic"].get("graduation_year") or "",
                "求职状态": res["basic"].get("status") or "",
                "学校": (first.get("school") or "").strip(),
                "专业": (first.get("major") or "").strip(),
                "学历": norm_degree(first.get("degree") or ""),
                "教育起止": (first.get("period") or "").strip(),
                "教育条数": len(education),
                "工作条数": len(work),
                "累计工作月数": total_months(work),
                "最近空档月数": gap_months(work) if gap_months(work) is not None else "",
                "工作经历全文": "\n".join(
                    f'{w.get("period", "")} {w.get("company", "")}/{w.get("title", "")}'
                    + ("：" + "；".join(w.get("descriptions") or []) if w.get("descriptions") else "")
                    for w in work),
                "缺失字段": "；".join(missing),
                "建议补充": "、".join(nice),
                "采集完整": "是" if ok else "否",
                "截图张数": len(images),
                "人事结论": label,
                "人事原因": (r.get("不合适原因") or r.get("合适原因") or "").replace("；", " / "),
            }
            for i in range(1, 11):
                if i <= len(work):
                    w = work[i - 1]
                    row[f"第{i}家公司"] = w.get("company", "")
                    row[f"第{i}家职位"] = w.get("title", "")
                    row[f"第{i}家起止"] = w.get("period", "")
                else:
                    row[f"第{i}家公司"] = row[f"第{i}家职位"] = row[f"第{i}家起止"] = ""

            rows.append(row)
            flag = "✓" if ok else "⚠"
            print(f"  [{len(rows)}/{len(jobs)}] {flag} {row['姓名'] or '(无名)':<8}"
                  f" 工作{row['工作条数']} 教育{row['教育条数']}"
                  f" 缺失:{row['缺失字段'][:26] or '无':<26} 人事={label}")

    if not rows:
        print("没有产出")
        return 1

    # 去重（同一人：姓名 + 学校）
    seen, dedup = set(), []
    dup = 0
    for row in rows:
        key = (row["姓名"], row["学校"])
        if row["姓名"] and key in seen:
            dup += 1
            continue
        if row["姓名"]:
            seen.add(key)
        dedup.append(row)
    for i, row in enumerate(dedup, 1):
        row["序号"] = i

    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(dedup[0]))
        writer.writeheader()
        writer.writerows(dedup)

    complete = sum(1 for r in dedup if r["采集完整"] == "是")
    verdict = {}
    for r in dedup:
        verdict[r["人事结论"]] = verdict.get(r["人事结论"], 0) + 1
    modes = {}
    for r in dedup:
        modes[r["学历性质"]] = modes.get(r["学历性质"], 0) + 1

    lines = [
        "运营助理 数据集清洗报告",
        "=" * 46,
        f"来源：{len(labels)} 条运营助理记录（人事表格），读取失败 {failed} 条",
        f"去重后 {len(dedup)} 条（重复 {dup} 条）",
        f"采集完整 {complete}/{len(dedup)}（{complete / len(dedup):.0%}）",
        f"人事结论：{verdict}",
        f"学历性质：{modes}",
        "",
        "缺失字段统计：",
    ]
    miss_count: dict[str, int] = {}
    for r in dedup:
        for m in (r["缺失字段"] or "").split("；"):
            if m:
                miss_count[m] = miss_count.get(m, 0) + 1
    for k, v in sorted(miss_count.items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"  {k}: {v}")
    if not miss_count:
        lines.append("  无")

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\n-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
