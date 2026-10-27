"""把人事发来的筛选表（未命名表格.xlsx）结构化，并从原因文本里统计规则。

    python tools/import_hr_labels.py

产出：
    data/hr-labels.csv      每行一位候选人的判定（招聘人/岗位/序号/结论/原因）
    data/hr-rules.json      从原因里统计出的高频规则（供筛选器使用）
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent     # 工作区根目录
XLSX = ROOT / "未命名表格.xlsx"
DATA = ROOT / "data"

# 原因文本 -> 规则标签
RULES = [
    ("非全日制", ["非全日制", "不是全日制", "全日制"]),
    ("学历不符", ["学历不符", "中专", "学历不", "大专及以上", "学历符合"]),
    ("年龄不符", ["年龄", "岁"]),
    ("空档期过长", ["空档", "空白", "无工作经历", "没有工作经验", "未确认工作"]),
    ("简历不完整", ["简历不完整", "简历不完善", "简历较空", "简历的过往", "写得很随意"]),
    ("同行", ["同行"]),
    ("经验太浅", ["经验太浅", "没有工作经验", "应届", "实习生", "明年毕业"]),
    ("岗位不匹配", ["岗位不匹配", "工作经历不符", "岗位不够匹配", "不适配", "履历不合适"]),
    ("稳定性差", ["任职周期", "在岗时间", "稳定性", "都较短"]),
    ("沟通类经验", ["客服", "销售", "沟通", "导购", "外贸", "售后"]),
    ("运营经验", ["运营"]),
    ("管理经验", ["管理", "团队", "SOP", "绩效"]),
]


def classify(text: str) -> list[str]:
    hits = []
    for name, keys in RULES:
        if any(k in text for k in keys):
            hits.append(name)
    return hits


def main() -> int:
    import openpyxl

    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    rows: list[dict] = []

    for sheet in wb.sheetnames:
        ws = wb[sheet]
        try:
            ws.reset_dimensions()      # read_only 模式下 dimension 常被写成 1x1，必须重置
        except Exception:
            pass
        header: list[str] = []
        for r, row in enumerate(ws.iter_rows(values_only=True), 1):
            values = [("" if c is None else str(c)).strip() for c in row]
            if not any(values):
                continue
            if r == 1 or (not header and any("原因" in v or "序号" in v for v in values)):
                header = values
                continue
            rec = {header[i] if i < len(header) else f"col{i}": v
                   for i, v in enumerate(values) if v}
            if not rec.get("序号"):
                continue
            bad = rec.get("不合适原因", "")
            good = rec.get("合适的原因", "")
            verdict = "合适" if good and not bad else ("不合适" if bad else "")
            rows.append({
                "招聘人": rec.get("招聘人") or sheet,
                "工作表": sheet,
                "序号": rec.get("序号", ""),
                "时间": rec.get("时间", ""),
                "岗位": rec.get("招聘岗位", ""),
                "结论": verdict,
                "不合适原因": bad.replace("\n", "；"),
                "合适原因": good.replace("\n", "；"),
                "命中规则": "/".join(classify(bad or good)),
            })
    wb.close()

    if not rows:
        print("没读到任何行，请检查表格结构")
        return 1

    DATA.mkdir(exist_ok=True)
    out = DATA / "hr-labels.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    # 统计
    verdicts = Counter(r["结论"] for r in rows)
    by_sheet = Counter(r["工作表"] for r in rows)
    by_job = Counter(r["岗位"] for r in rows)
    rule_hits = Counter()
    for r in rows:
        for tag in (r["命中规则"] or "").split("/"):
            if tag:
                rule_hits[tag] += 1

    print(f"共 {len(rows)} 条判定 -> {out}")
    print(f"  招聘人/工作表: {dict(by_sheet)}")
    print(f"  岗位: {dict(by_job)}")
    print(f"  结论: {dict(verdicts)}")
    print("\n  规则命中次数（不合适原因里的高频理由）:")
    for tag, n in rule_hits.most_common():
        print(f"    {tag:<12} {n}")

    # 抽出可执行的硬规则
    rules = {
        "学历": {
            "要求": "全日制大专及以上",
            "反例关键词": ["非全日制", "中专", "高中"],
        },
        "年龄": {"运营助理上限": 30, "建议范围": "22-28", "反例": ["35岁", "34岁", "年龄偏大"]},
        "空档期": {"可接受": "0.5 年内", "反例": ["空档期过长", "近半年简历空白"]},
        "经验": {"最低": "0.5 年以上", "排除": ["应届生", "实习生", "明年毕业"]},
        "同行": {"排除": ["一硕", "云上源"], "说明": "同行公司不招"},
        "稳定性": {"反例": ["每份工作任职周期都较短", "在岗时间都较短"]},
        "岗位匹配": {"加分": ["客服", "销售", "运营", "导购", "外贸", "售后"], "扣分": ["生产文员", "体制内文员"]},
        "统计": {"规则命中": dict(rule_hits)},
    }
    (DATA / "hr-rules.json").write_text(json.dumps(rules, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(f"\n  规则文件 -> {DATA / 'hr-rules.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
