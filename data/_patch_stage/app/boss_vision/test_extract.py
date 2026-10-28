"""结构化提取的回归测试：用已导出的 OCR 行跑解析，不调模型，秒级完成。

    python test_extract.py

6 份真实简历（tests/fixtures/ex*.png 对应的 ocr_ex*.json）各自断言：
公司/职位/时间/描述、学校/专业/学历/时间 是否都提取到位。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import extract
import vision

FIX = _HERE / "tests" / "fixtures"
results: list[tuple[str, bool, str]] = []

# 每份样本的期望（人工核对过）
EXPECT = {
    "ex1": {"work": 5, "edu": 2, "company": "福建飞鸟传媒有限公司", "school": "岳阳学院"},
    "ex2": {"work": 3, "edu": 1, "company": "福州中榕网络科技有限公司", "school": "龙岩学院"},
    "ex3": {"work": 5, "edu": 1, "company": "福州尹兴电子商务有限公司", "school": "泉州职业技术大学"},
    "ex4": {"work": 2, "edu": 1, "company": "树人托管", "school": "福州外语外贸学院"},
    "ex5": {"work": 1, "edu": 1, "company": "韵莱电子商务有限公司", "school": "闽北职业技术学院"},
    "ex6": {"work": 1, "edu": 1, "company": "北京一合广告公司", "school": "衡水学院"},
}


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def load(name: str) -> tuple[list[dict], vision.Rect | None]:
    data = json.loads((FIX / f"ocr_{name}.json").read_text(encoding="utf-8"))
    wide = data.get("wide")
    rect = vision.Rect(wide["x"], wide["y"], wide["w"], wide["h"]) if wide else None
    return data["lines"], rect


def main() -> int:
    print("结构化提取 —— 回归测试（免 OCR）")
    for name, exp in EXPECT.items():
        path = FIX / f"ocr_{name}.json"
        if not path.exists():
            check(f"{name}: OCR 转储存在", False, "先跑 dump_ocr.py")
            continue
        print(f"\n[{name}]")
        lines, wide = load(name)
        res = extract.extract(lines, wide)

        check(f"{name} 识别到工作经历小节", "工作经历" in res.sections_found, str(res.sections_found))
        check(f"{name} 识别到教育经历小节", "教育经历" in res.sections_found)
        check(f"{name} 工作经历 {exp['work']} 条", len(res.work) == exp["work"], f"实际 {len(res.work)}")
        check(f"{name} 教育经历 {exp['edu']} 条", len(res.education) == exp["edu"], f"实际 {len(res.education)}")

        companies = [w.company for w in res.work]
        check(f"{name} 抓到公司名 {exp['company']}", exp["company"] in companies, str(companies[:3]))
        check(f"{name} 每条都有时间", all(extract.DATE_RE.search(w.period) for w in res.work),
              str([w.period for w in res.work][:3]))
        check(f"{name} 每条都有描述或标签",
              all(w.descriptions or w.tags for w in res.work))

        schools = [e.school for e in res.education]
        check(f"{name} 抓到学校 {exp['school']}", exp["school"] in schools, str(schools))
        check(f"{name} 学历有值", all(e.degree for e in res.education), str([e.degree for e in res.education]))
        check(f"{name} 教育时间有值", all(extract.DATE_RE.search(e.period) for e in res.education),
              str([e.period for e in res.education]))

    # 一个纯函数级的边界用例
    print("\n[边界]")
    check("时间格式 2023.06-至今", bool(extract.DATE_RE.search("2023.06-至今")))
    check("时间格式 2019 - 2023", bool(extract.DATE_RE.search("2019 - 2023")))
    check("时间格式 2021-2025（无空格）", bool(extract.DATE_RE.search("2021-2025")))
    check("普通文本不会误判成时间", not extract.DATE_RE.search("负责运营和投放"))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{'=' * 50}\n{passed}/{len(results)} 通过")
    for n, d in failed:
        print(f"  FAIL: {n} — {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
