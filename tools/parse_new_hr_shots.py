"""把新人事表格里的「运营助理」记录解析成结构化数据集。

输入：
  data/新人事-运营助理.csv          清洗后的标签（合适/不合适 + 原因）
  data/新人事截图/<招聘人>/*.jpg    按行导出的简历截图

输出：
  data/新人事-解析.csv              与 data/运营助理-数据集.csv 同结构
  （--merge 时）data/运营助理-数据集.csv 合并去重后的总表

用法：
  python tools\\parse_new_hr_shots.py --limit 3      # 先试 3 条
  python tools\\parse_new_hr_shots.py                # 全量
  python tools\\parse_new_hr_shots.py --merge        # 解析后并入总数据集
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))
sys.path.insert(0, str(REPO / "tools"))

import numpy as np                                    # noqa: E402
from PIL import Image                                 # noqa: E402

import judge                                          # noqa: E402
import resume_reader                                  # noqa: E402
import vlm                                            # noqa: E402

from build_ops_dataset import (                       # noqa: E402
    gap_months, norm_degree, norm_mode, total_months,
)

SRC_CSV = REPO / "data" / "新人事-运营助理.csv"
SHOT_DIR = REPO / "data" / "新人事截图"
OUT_CSV = REPO / "data" / "新人事-解析.csv"
DATASET_CSV = REPO / "data" / "运营助理-数据集.csv"

SLOTS = 10


def images_of(row: dict) -> list[Path]:
    """按文件名里的 k 排序，取这一行的全部截图。"""
    d = SHOT_DIR / row["招聘人"]
    if not d.exists():
        return []
    pat = f"{row['招聘人']}-{row['序号']}-r{row['行号']}-"
    files = [p for p in d.glob(f"{pat}*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    def key(p: Path) -> int:
        m = re.search(r"-(\d+)\.\w+$", p.name)
        return int(m.group(1)) if m else 0
    return sorted(files, key=key)


def parse_row(row: dict, cfg: dict, jcfg: dict) -> dict:
    out = {
        "序号": row["序号"], "招聘人": row["招聘人"], "表内序号": row["行号"],
        "人事结论": row["判定"], "人事原因": row["原因原文"],
        "截图张数": row["截图数"],
    }
    files = images_of(row)
    if not files:
        out["采集完整"] = "否"
        out["缺失字段"] = "无截图"
        return out

    crops = []
    for path in files[:6]:
        try:
            crops.append(np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy())
        except Exception:
            continue
    if not crops:
        out["采集完整"] = "否"
        out["缺失字段"] = "截图读取失败"
        return out

    # OCR 并行
    texts: list[str] = []
    with ThreadPoolExecutor(max_workers=min(4, len(crops))) as pool:
        futures = [pool.submit(vlm.read_screen_text, c, cfg) for c in crops]
        for fut in futures:
            try:
                texts.append((fut.result() or {}).get("text", ""))
            except Exception:
                texts.append("")

    lines: list[str] = []
    for t in texts:
        for ln in (t or "").splitlines():
            ln = ln.strip()
            if ln and (not lines or ln != lines[-1]):
                lines.append(ln)
    full_text = "\n".join(lines)

    data = {}
    try:
        data = (judge.structure_text(full_text, jcfg) or {}).get("data") or {}
    except Exception as exc:
        out["采集完整"] = "否"
        out["缺失字段"] = f"结构化失败：{exc}"[:40]
        return out

    parts = vlm.normalize(data.get("sections"))
    work = parts.get("work") or []
    education = parts.get("education") or []
    basic = dict(data.get("basic") or {})

    # 文本兜底：年龄 / 学习形式 / 状态
    try:
        for k, v in resume_reader._basic_from_text(full_text).items():
            if not basic.get(k):
                basic[k] = v
    except Exception:
        pass

    # 工作经历兜底
    if not work:
        try:
            work = resume_reader._heuristic_work_entries(full_text)
        except Exception:
            pass
    try:
        resume_reader._fill_company_from_text(work, texts, log=lambda *_: None)
    except Exception:
        pass

    edu0 = education[0] if education else {}
    periods = [w for w in work if isinstance(w, dict) and (w.get("period") or "").strip()]
    out.update({
        "姓名": (data.get("name") or "").strip(),
        "年龄": basic.get("age", ""),
        "学历性质": norm_mode(basic.get("education_mode", "")),
        "毕业年份": basic.get("graduation_year", ""),
        "求职状态": basic.get("status", ""),
        "学校": edu0.get("school", ""),
        "专业": edu0.get("major", ""),
        "学历": norm_degree(edu0.get("degree", "")),
        "教育起止": edu0.get("period", ""),
        "教育条数": len(education),
        "工作条数": len(work),
        "累计工作月数": total_months(periods),
        "最近空档月数": gap_months(periods),
        "工作经历全文": "\n".join(
            f"{w.get('period','')} {w.get('company','')} {w.get('title','')}\n"
            + "\n".join(w.get("descriptions") or []) for w in work)[:2000],
    })
    for i in range(1, SLOTS + 1):
        w = work[i - 1] if i - 1 < len(work) else {}
        out[f"第{i}家公司"] = w.get("company", "")
        out[f"第{i}家职位"] = w.get("title", "")
        out[f"第{i}家起止"] = w.get("period", "")

    # 完整性（复用线上判定）
    try:
        import completeness
        ok, missing, nice = completeness.check({
            "name": out["姓名"], "basic": basic, "education": education,
            "work": work, "collection": {"complete": bool(work or education)}})
        out["采集完整"] = "是" if ok else "否"
        out["缺失字段"] = "；".join(missing)
        out["建议补充"] = "；".join(nice)
    except Exception:
        out["采集完整"] = "是" if (work or education) else "否"
    return out


def merge_into_dataset(new_rows: list[dict], log=print) -> int:
    """把解析结果并入总数据集，按 (招聘人, 序号, 原因前 30 字) 去重。"""
    old = list(csv.DictReader(open(DATASET_CSV, encoding="utf-8-sig"))) if DATASET_CSV.exists() else []
    fields = list(old[0].keys()) if old else list(new_rows[0].keys())
    for r in new_rows:
        for k in r:
            if k not in fields:
                fields.append(k)

    def key(r: dict) -> tuple:
        reason = (r.get("人事原因") or "").strip()[:30]
        return (r.get("招聘人", ""), str(r.get("表内序号") or r.get("序号", "")), reason)

    seen = {key(r) for r in old}
    merged = list(old)
    added = 0
    for r in new_rows:
        k = key(r)
        if k in seen:
            continue
        seen.add(k)
        merged.append({f: r.get(f, "") for f in fields})
        added += 1

    DATASET_CSV.write_text("", encoding="utf-8")
    with open(DATASET_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged)
    log(f"合并完成：旧 {len(old)} + 新增 {added} = {len(merged)} 条 -> {DATASET_CSV}")
    return len(merged)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(SRC_CSV, encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]
    cfg = vlm.load_config()
    jcfg = judge.load_config()

    out_rows: list[dict] = []
    for i, row in enumerate(rows, 1):
        res = parse_row(row, cfg, jcfg)
        out_rows.append(res)
        print(f"  [{i}/{len(rows)}] {row['招聘人']}-{row['序号']} "
              f"工作{res.get('工作条数','?')} 教育{res.get('教育条数','?')} "
              f"{res.get('人事结论','')} {res.get('缺失字段','')[:20]}", flush=True)

    fields = []
    for r in out_rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"\n解析结果：{OUT_CSV}（{len(out_rows)} 条）")

    if args.merge:
        merge_into_dataset(out_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
