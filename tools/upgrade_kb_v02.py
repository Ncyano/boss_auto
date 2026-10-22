"""把知识库升级到 v0.2.0：年龄 / 空档 / 同行 / 应届 全部改为硬性不合适条件。

    python tools/upgrade_kb_v02.py

做法：
    · P01 半年经验     -> confirmed_by_user + hard（min_months = 6）
    · P02 履历空白     -> confirmed_by_user + hard（max_gap_months = 6）
    · P03 同行限制     -> confirmed_by_user + hard（blacklist 四条）
    · P04 在校/实习    -> confirmed_by_user + hard（关键词三条）
    · 新增 A01 年龄上限 -> confirmed_by_user + hard（max_age = 30）
    · X01 年龄排除     -> 标记 superseded_by_A01（保留审计痕迹）
    · 版本 0.1.0 -> 0.2.0，更新 manifest 哈希
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "运营助理知识库_v0.1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    rules_path = KB / "rules.json"
    data = json.loads(rules_path.read_text(encoding="utf-8"))

    by_id = {r["id"]: r for r in data["rules"]}

    def harden(rule_id: str, condition: str, action: str, params: dict,
               evidence: str = "简历原文或结构化字段") -> None:
        rule = by_id[rule_id]
        rule["kind"] = "hard"
        rule["status"] = "confirmed_by_user"
        rule["condition"] = condition
        rule["action"] = action
        rule["evidence_required"] = evidence
        rule["if_unknown"] = "review"
        rule["params"] = params
        rule.setdefault("confirmed_at", str(date.today()))
        rule["notes"] = (rule.get("notes") or "") + \
            "2026-10 用户确认升级为硬性不合适条件（v0.2.0）。"

    harden("P01", "相关工作经验累计不足 6 个月（0.5 年）；实习是否计入按岗位口径",
           "不合适；记录经验门槛未满足",
           {"min_months": 6, "count": "累计相关工作月数"})

    harden("P02", "从最近一段工作结束到今天，空档超过 6 个月",
           "不合适；记录空档期过长",
           {"max_gap_months": 6, "basis": "今天", "current_job_gap": 0},
           evidence="工作经历起止原文")

    harden("P03", "任职公司命中同行名单（按公司全称包含匹配）",
           "不合适；记录同行",
           {"blacklist": ["一硕", "一数", "云上源", "谷一科技"],
            "match": "公司名包含关键词"})

    harden("P04", "求职状态或履历显示尚未毕业、在校或实习中",
           "不合适；记录在校/实习",
           {"keywords": ["在校", "应届", "实习", "在读"]})

    # 年龄：新增硬规则，原 X01 只保留审计痕迹
    if "A01" not in by_id:
        age_rule = {
            "id": "A01",
            "title": "年龄上限",
            "kind": "hard",
            "status": "confirmed_by_user",
            "condition": "候选人年龄超过岗位上限（运营助理 30 岁）",
            "action": "不合适；记录年龄超限",
            "evidence_required": "简历顶部年龄原文",
            "if_unknown": "review",
            "params": {"max_age": 30, "recommended_range": [22, 28],
                       "job": "运营助理"},
            "sources": (by_id.get("X01", {}).get("sources") or []),
            "notes": "2026-10 用户确认：年龄作为硬性不合适条件（v0.2.0），"
                     "取代原 X01「年龄不作为特征」的排除口径。",
            "confirmed_at": str(date.today()),
        }
        # 插到 W06 之后，保持编号可读
        idx = next((i for i, r in enumerate(data["rules"]) if r["id"] == "W06"), len(data["rules"]) - 1)
        data["rules"].insert(idx + 1, age_rule)

    if "X01" in by_id:
        by_id["X01"]["status"] = "superseded"
        by_id["X01"]["kind"] = "superseded"
        by_id["X01"]["superseded_by"] = "A01"
        by_id["X01"]["action"] = "已被 A01 取代；仅保留审计原文"
        by_id["X01"]["notes"] = (by_id["X01"].get("notes") or "") + \
            "2026-10 用户确认年龄作为硬规则，本规则由 A01 取代（v0.2.0）。"

    data["version"] = "0.2.0"
    data["updated_date"] = str(date.today())
    data["changelog"] = data.get("changelog", []) + [{
        "version": "0.2.0",
        "date": str(date.today()),
        "change": "P01/P02/P03/P04 与年龄(A01) 升级为硬性不合适条件；X01 标记 superseded",
        "confirmed_by": "user",
    }]
    data["precedence"] = [
        "学历硬规则失败（E01/E02）或确认无工作经历（W06）：不合适，工作模型不能覆盖",
        "年龄超限（A01）、空档超期（P02）、同行（P03）、在校实习（P04）、经验不足（P01）：不合格，直接淘汰",
        "存在阻碍判断的采集/身份/学历未知：待复核",
        "以上均通过后逐条检查工作证据（W01–W05）；无法判断不能当作不满足",
    ]

    rules_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # manifest 哈希更新
    man_path = KB / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["version"] = "0.2.0"
    man["rules"] = len(data["rules"])
    files = man.get("files") or {}
    for name in list(files):
        p = KB / name
        if p.exists():
            files[name] = sha256(p)
    man["files"] = files
    man["changelog"] = data["changelog"]
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"知识库已升级到 {data['version']}，规则 {len(data['rules'])} 条")
    for r in data["rules"]:
        if r["kind"] == "hard":
            print(f"  硬规则 {r['id']:<4} {r['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
