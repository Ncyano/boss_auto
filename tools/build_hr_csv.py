"""把人事表格里的简历截图，转成和我们采集出来的 CSV 同样的格式，末尾追加岗位与是否合适。

    python tools\\build_hr_csv.py --limit 3      # 先跑 3 行试水
    python tools\\build_hr_csv.py               # 全量（106 张图，约 10~30 分钟）

输入：
    data\\hr-labels.csv      人事判定（招聘人/序号/岗位/结论/原因）
    data\\hr-shot-map.json   工作表 -> 行号 -> 截图（tools\\extract_hr_shots.py 产出）
    boss_vision\\runs\\hr-shots\\<工作表>\\r<行>.png

输出：
    data\\hr-resumes-采集格式.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import numpy as np  # noqa: E402
import vlm  # noqa: E402
from PIL import Image  # noqa: E402

import export_csv  # noqa: E402

DATA = ROOT / "data"
SHOTS = ROOT / "boss_vision" / "runs" / "hr-shots"
OUT = DATA / "hr-resumes-采集格式.csv"


def read_hr_labels() -> dict[tuple[str, str], dict]:
    """(工作表, 序号) -> 人事判定"""
    path = DATA / "hr-labels.csv"
    out: dict[tuple[str, str], dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            out[(row["工作表"], str(row["序号"]).strip())] = row
    return out


def read_shot_map() -> dict[str, dict[str, int]]:
    return json.loads((DATA / "hr-shot-map.json").read_text(encoding="utf-8"))


def read_one_sheet(images: list[Path], cfg: dict) -> dict:
    """读一位候选人的所有截图，合并成一条记录。"""
    work: list[dict] = []
    education: list[dict] = []
    name = ""
    basic: dict = {}
    tokens = 0

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
            # 「非全日制」用 OCR 硬校验，模型容易把它读成「全日制」
            forced = vlm.scan_education_mode(bgr)
            if forced:
                basic["education_mode"] = forced
                basic["education_mode_source"] = "ocr"
        parts = vlm.normalize(raw["sections"])
        vlm.merge_screens(work, parts["work"])
        for e in parts["education"]:
            same = next((o for o in education
                         if not e.get("school") or not o.get("school")
                         or e["school"] == o["school"]), None)
            if same is None:
                education.append(e)
            else:
                for field in ("school", "major", "degree", "period"):
                    if not (same.get(field) or "").strip() and (e.get(field) or "").strip():
                        same[field] = e[field]

    return {"name": name, "basic": basic, "work": work, "education": education,
            "tokens": tokens}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 行（试水用）")
    ap.add_argument("--workers", type=int, default=4, help="并发读图数")
    args = ap.parse_args(argv)

    labels = read_hr_labels()
    shots = read_shot_map()
    cfg = vlm.load_config()
    if not vlm.api_key(cfg):
        print("没有配置 VLM_API_KEY")
        return 2

    jobs: list[tuple[str, str, int, list[Path], dict]] = []
    for sheet, rows in shots.items():
        for row_no, _medias in sorted(rows.items()):
            folder = SHOTS / sheet
            images = sorted(folder.glob(f"r{row_no}-*"))
            if not images:
                continue
            hr_index = str(int(row_no) - 1)          # Excel 第 2 行 = 序号 1
            label = labels.get((sheet, hr_index), {})
            jobs.append((sheet, row_no, int(row_no), images, label))

    if args.limit:
        jobs = jobs[: args.limit]
    print(f"待处理 {len(jobs)} 位候选人，共 {sum(len(j[3]) for j in jobs)} 张截图\n")

    def run(job):
        sheet, row_no, _idx, images, label = job
        res = read_one_sheet(images, cfg)
        return sheet, row_no, images, label, res

    records: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for sheet, row_no, images, label, res in pool.map(run, jobs):
            done += 1
            if res.get("error"):
                print(f"  [{done}/{len(jobs)}] {sheet} 行{row_no} 失败：{res['error'][:60]}")
                continue
            work = res["work"]
            education = res["education"]
            needs = []
            if not res["name"]:
                needs.append("姓名未识别")
            if not education:
                needs.append("未读到教育经历")
            miss_c = sum(1 for w in work if not (w.get("company") or "").strip())
            if miss_c:
                needs.append(f"{miss_c} 段经历缺公司名")

            rec = {
                "index": len(records) + 1,
                "name": res["name"],
                "basic": res["basic"],
                "opened": True,
                "work": work,
                "education": education,
                "complete": bool(education),
                "screen_count": len(images),
                "section_seen": ["work"] if work else [],
                "conflicts": [],
                "name_candidates": [res["name"]] if res["name"] else [],
                "needs_review": bool(needs),
                "review_reason": "；".join(needs),
                "reader": "vlm",
            }
            row = export_csv.row_of(rec)
            row.update({
                "招聘人": label.get("招聘人", sheet),
                "岗位": label.get("岗位", ""),
                "是否合适": label.get("结论", ""),
                "人事原因": (label.get("不合适原因") or label.get("合适原因") or "").replace("；", " / "),
                "截图张数": len(images),
            })
            records.append(row)
            print(f"  [{done}/{len(jobs)}] {sheet} 行{row_no} -> {res['name'] or '(无名)'}"
                  f" 工作{len(work)}段 教育{len(education)}段"
                  f" | 岗位={label.get('岗位', '')} 结论={label.get('结论', '')}")

    if not records:
        print("没有产出任何记录")
        return 1

    cols = list(records[0].keys())
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        writer.writerows(records)

    print(f"\n已写出 {len(records)} 条 -> {OUT}")
    ok = sum(1 for r in records if (r.get("是否合适") or "") == "合适")
    no = sum(1 for r in records if (r.get("是否合适") or "") == "不合适")
    print(f"  人事结论：合适 {ok} / 不合适 {no} / 未标 {len(records) - ok - no}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
