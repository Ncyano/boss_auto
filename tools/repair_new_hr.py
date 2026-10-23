"""补采不完整记录：放大重读 + 结构化，补齐缺失字段。

用法：
  python tools/repair_new_hr.py            # 只补 采集完整=否 的记录
  python tools/repair_new_hr.py --all      # 全部重跑（慢）
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))
sys.path.insert(0, str(REPO / "tools"))

import numpy as np
from PIL import Image

import completeness
import judge
import resume_reader
import vlm
from build_ops_dataset import gap_months, norm_degree, norm_mode, total_months
from parse_new_hr_shots import images_of

SRC = REPO / "data" / "新人事-解析.csv"


def deep_read(paths: list[Path], cfg: dict, jcfg: dict) -> dict:
    """放大 2 倍 OCR + 结构化，尽量把字读全。"""
    texts = []
    for p in paths[:6]:
        try:
            bgr = np.asarray(Image.open(p).convert("RGB"))[:, :, ::-1].copy()
            zo = dict(cfg)
            zo["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
            texts.append((vlm.read_screen_text(bgr, zo) or {}).get("text", ""))
        except Exception:
            continue
    full = "\n".join(dict.fromkeys([ln.strip() for t in texts for ln in (t or "").splitlines() if ln.strip()]))
    data = {}
    try:
        data = (judge.structure_text(full, jcfg) or {}).get("data") or {}
    except Exception:
        pass
    parts = vlm.normalize(data.get("sections"))
    basic = dict(data.get("basic") or {})
    for k, v in resume_reader._basic_from_text(full).items():
        basic.setdefault(k, v)
    work = parts.get("work") or []
    if not work:
        work = resume_reader._heuristic_work_entries(full)
    resume_reader._fill_company_from_text(work, texts, log=lambda *_: None)
    return {"name": (data.get("name") or "").strip(), "basic": basic,
            "work": work, "education": parts.get("education") or [], "text": full}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
    fields = list(rows[0].keys())
    cfg, jcfg = vlm.load_config(), judge.load_config()

    targets = [r for r in rows if args.all or (r.get("采集完整") or "") == "否"]
    print(f"待补采 {len(targets)} 条")

    fixed = 0
    for i, r in enumerate(targets, 1):
        paths = images_of(r)
        if not paths:
            print(f"  [{i}] {r['招聘人']}-{r['序号']} 没有截图，跳过")
            continue
        got = deep_read(paths, cfg, jcfg)
        edu = got["education"]
        e0 = edu[0] if edu else {}
        work = got["work"]
        ok, missing, nice = completeness.check({
            "name": got["name"] or r.get("姓名", ""), "basic": got["basic"],
            "education": edu, "work": work, "collection": {"complete": True}})
        before = (r.get("采集完整") or "") == "是"

        if got["name"] and not (r.get("姓名") or "").strip():
            r["姓名"] = got["name"]
        for src_key, dst in (("school", "学校"), ("major", "专业"), ("degree", "学历"),
                             ("period", "教育起止")):
            if not (r.get(dst) or "").strip() and (e0.get(src_key) or "").strip():
                r[dst] = (norm_degree(e0[src_key]) if dst == "学历" else e0[src_key])
        for src_key, dst in (("age", "年龄"), ("graduation_year", "毕业年份"),
                             ("status", "求职状态")):
            if not str(r.get(dst) or "").strip() and str(got["basic"].get(src_key) or "").strip():
                r[dst] = got["basic"][src_key]
        if not (r.get("学历性质") or "").strip() or r.get("学历性质") == "未知":
            m = norm_mode(got["basic"].get("education_mode", ""))
            if m != "未知":
                r["学历性质"] = m
        # 逐字段合并：即使条数没变，补回的职责/公司/起止也要写回
        merged_any = False
        for k in range(1, 11):
            w = work[k - 1] if k - 1 < len(work) else {}
            for src, dst in (("company", "公司"), ("title", "职位"), ("period", "起止")):
                key = f"第{k}家{dst}"
                new_val = (w.get(src) or "").strip()
                if new_val and not str(r.get(key) or "").strip():
                    r[key] = new_val
                    merged_any = True
        if work and (merged_any or int(r.get("工作条数") or 0) < len(work)):
            r["工作条数"] = max(int(r.get("工作条数") or 0), len(work))
            r["教育条数"] = len(edu)
            periods = [w.get("period") or "" for w in work if (w.get("period") or "").strip()]
            r["累计工作月数"] = total_months(periods)
            r["最近空档月数"] = gap_months(periods)
            r["工作经历全文"] = "\n".join(
                f"{w.get('period','')} {w.get('company','')} {w.get('title','')}\n"
                + "\n".join(w.get("descriptions") or []) for w in work)[:2000]
            for k in range(1, 11):
                w = work[k - 1] if k - 1 < len(work) else {}
                r[f"第{k}家公司"] = w.get("company", "")
                r[f"第{k}家职位"] = w.get("title", "")
                r[f"第{k}家起止"] = w.get("period", "")
        # 用"最终要保存的字段"重算完整性，避免 mark 成功但内容没落盘
        ok2, missing2, nice2 = completeness.check({
            "name": r.get("姓名", ""),
            "basic": {"age": r.get("年龄", ""), "education_mode": r.get("学历性质", ""),
                      "graduation_year": r.get("毕业年份", ""), "status": r.get("求职状态", "")},
            "education": ([{"school": r.get("学校", ""), "degree": r.get("学历", ""),
                            "major": r.get("专业", ""), "period": r.get("教育起止", "")}]
                          if (r.get("学校") or r.get("学历")) else []),
            "work": [{"company": r.get(f"第{k}家公司", ""), "title": r.get(f"第{k}家职位", ""),
                      "period": r.get(f"第{k}家起止", "")}
                     for k in range(1, 11) if (r.get(f"第{k}家公司") or r.get(f"第{k}家职位"))],
            "collection": {"complete": True}})
        ok, missing, nice = ok2, missing2, nice2
        r["采集完整"] = "是" if ok else "否"
        r["缺失字段"] = "；".join(missing)
        r["建议补充"] = "；".join(nice)
        if not before and ok:
            fixed += 1
        mark = "✓" if ok else "·"
        print(f"  [{i}/{len(targets)}] {mark} {r['招聘人']}-{r['序号']} "
              f"姓名={r.get('姓名','')[:6]} 学校={r.get('学校','')[:10]} "
              f"工作{r.get('工作条数')} 缺={r['缺失字段'][:18]}", flush=True)

    with open(SRC, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    left = sum(1 for r in rows if (r.get("采集完整") or "") == "否")
    print(f"\n补采完成：修复 {fixed} 条；仍不完整 {left} 条（共 {len(rows)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
