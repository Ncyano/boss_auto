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
        # 按"经历身份"合并：旧记录优先，新的只补空字段；绝不整段覆盖
        merged_any = False
        def _slots(row, n=10):
            out = []
            for k in range(1, n + 1):
                c = (row.get(f"第{k}家公司") or "").strip()
                t = (row.get(f"第{k}家职位") or "").strip()
                p_ = (row.get(f"第{k}家起止") or "").replace(" ", "").strip()
                if c or t or p_:
                    out.append({"company": c, "title": t, "period": p_,
                                "descriptions": []})
            return out

        def _ident(e):
            return ((e.get("company") or "").strip(),
                    (e.get("period") or "").replace(" ", "").strip())

        def _match(entries, e):
            """精确匹配优先；旧记录缺字段时按公司找唯一候选；有歧义就不合并。"""
            comp = (e.get("company") or "").strip()
            per = (e.get("period") or "").replace(" ", "").strip()
            exact = [m for m in entries if _ident(m) == (comp, per) and comp]
            if len(exact) == 1:
                return exact[0]
            if not comp:
                return None
            def _conflict(old, per):
                op = (old.get("period") or "").replace(" ", "").strip()
                return bool(op and per and op != per)

            same_comp = [m for m in entries if (m.get("company") or "").strip() == comp]
            if len(same_comp) == 1 and not _conflict(same_comp[0], per):
                return same_comp[0]                     # 公司唯一且时间不冲突 -> 补齐缺字段
            if per:
                cand = [m for m in same_comp
                        if not (m.get("period") or "").strip()]
                if len(cand) == 1:
                    return cand[0]
            return None                                  # 有歧义 -> 不动

        old_entries = _slots(r)
        merged = [dict(e) for e in old_entries]
        for e in work:
            same = _match(merged, e)
            if same is None:
                # 只要没出现过就追加（不会覆盖旧记录）
                merged.append({"company": (e.get("company") or "").strip(),
                               "title": (e.get("title") or "").strip(),
                               "period": (e.get("period") or "").strip(),
                               "descriptions": list(e.get("descriptions") or [])})
            else:
                if not same.get("descriptions") and e.get("descriptions"):
                    same["descriptions"] = list(e.get("descriptions") or [])
                    merged_any = True
                for fld in ("company", "title", "period"):
                    if not (same.get(fld) or "").strip() and (e.get(fld) or "").strip():
                        same[fld] = e.get(fld).strip()
                        merged_any = True

        if merged and (merged_any or len(merged) > len(old_entries)):
            r["工作条数"] = len(merged)
            r["教育条数"] = len(edu) or r.get("教育条数", "")
            for k in range(1, 11):
                e = merged[k - 1] if k - 1 < len(merged) else {}
                r[f"第{k}家公司"] = e.get("company", "")
                r[f"第{k}家职位"] = e.get("title", "")
                r[f"第{k}家起止"] = e.get("period", "")
            # 全文与月数一律从合并结果重建
            texts_all = []
            for e in merged:
                descs = [d for d in (e.get("descriptions") or []) if d]
                texts_all.append(f"{e.get('period','')} {e.get('company','')} {e.get('title','')}"
                                 + ("\n" + "\n".join(descs) if descs else ""))
            new_text = "\n".join(texts_all)[:2000]
            old_text = (r.get("工作经历全文") or "").strip()
            if old_text and len(new_text) < len(old_text):
                pass          # 新全文比旧文短 -> 保留原全文，不覆盖已有职责
            else:
                r["工作经历全文"] = new_text
            periods = [e.get("period") or "" for e in merged if (e.get("period") or "").strip()]
            r["累计工作月数"] = total_months(periods)
            r["最近空档月数"] = gap_months(periods)

        # 用"合并后真正要保存的字段"重算完整性
        full_text = (r.get("工作经历全文") or "").strip()

        def _duties_of(company: str, period: str) -> list[str]:
            """从全文里按公司名切出这一段，切到了才算有职责。"""
            if not full_text or not company:
                return []
            lines_all = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
            idx = next((i for i, ln in enumerate(lines_all) if company in ln), -1)
            if idx < 0:
                return []
            body: list[str] = []
            for ln in lines_all[idx + 1:]:
                # 遇到下一段的"起止 + 公司"行就停
                if any(k in ln for k in ("公司", "集团", "中心", "工作室")) and (
                        "20" in ln or "19" in ln):
                    break
                if ln.startswith(("教育经历", "项目经历", "校园经历", "专业技能")):
                    break
                body.append(ln)
            return body if sum(len(x) for x in body) >= 8 else []

        final_work = []
        for k in range(1, 11):
            c = (r.get(f"第{k}家公司") or "").strip()
            t = (r.get(f"第{k}家职位") or "").strip()
            pp = (r.get(f"第{k}家起止") or "").strip()
            if c or t or pp:
                final_work.append({"company": c, "title": t, "period": pp,
                                   "descriptions": _duties_of(c, pp)})
        final_edu = ([{"school": r.get("学校", ""), "degree": r.get("学历", ""),
                       "major": r.get("专业", ""), "period": r.get("教育起止", "")}]
                     if (r.get("学校") or r.get("学历")) else [])
        ok2, missing2, nice2 = completeness.check({
            "name": r.get("姓名", ""),
            "basic": {"age": r.get("年龄", ""), "education_mode": r.get("学历性质", ""),
                      "graduation_year": r.get("毕业年份", ""), "status": r.get("求职状态", "")},
            "education": final_edu, "work": final_work,
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
