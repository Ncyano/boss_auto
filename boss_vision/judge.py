"""工作经历证据匹配：交给大模型（DeepSeek），按知识库 W01–W05 逐条取证。

分工：
    · 千问 VL       读在线简历截图 -> 结构化
    · kb.py         执行知识库硬规则（E01 学历/E02 全日制/W06 无经历）与待复核判定
    · DeepSeek      按 W01–W05 逐条取证 -> 建议合适 / 不合适 / 待复核 + 原文引用

本模块不做淘汰决策，只产出"证据 + 建议"，最终结论由 kb.evaluate + 本模块合成。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import kb as kbmod

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "llm-config.json"

DEFAULT_CONFIG = {
    "_说明": "工作经历证据匹配用的文本模型。key 放环境变量 DEEPSEEK_API_KEY，或填 api_key。",
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key_env": "DEEPSEEK_API_KEY",
    "api_key": "",
    "temperature": 0,
    "max_tokens": 1200,
    "timeout": 120,
    "retries": 2,
}

SYSTEM_PROMPT = """你是运营助理岗位的简历证据抽取与匹配助手。

输入是候选人【工作经历】结构化数据，以及知识库中已启用的工作证据规则（W01–W05）。
你的任务：逐条判断规则是否满足，并引用简历原文作为证据。

硬性要求：
1. 只依据给定文本，不得编造。证据必须逐字来自简历，并标注经历序号。
2. 原文未提及不等于不存在；判断不了就输出"无法判断"，不得当成"明确不满足"。
3. 不得仅凭职位名称、行业名称下结论（"文员""新媒体"本身不是淘汰项）。
4. 不得使用年龄、性别、姓名、头像、推测的性格作为依据。
5. 不得自行设置权重、阈值或概率，不输出置信度分数。
6. 只输出 JSON，不要 markdown 代码块，不要解释。

输出格式：
{
  "rule_results": [
    {"rule_id": "W01", "status": "满足|部分满足|明确不满足|无法判断",
     "evidence": [{"quote": "原文片段", "experience_index": 1}],
     "reason": "一句话说明"}
  ],
  "missing_information": ["缺少什么信息"],
  "final_recommendation": "建议合适|不合适|待复核",
  "decision_basis": "一句话总结依据"
}

判定口径：
- 至少一条 W01–W04 为"满足"，且没有"明确不满足"的核心项 -> 建议合适
- 有职责层面的"明确不满足"（有原文证据说明不具备客户沟通/运营类职责） -> 不合适
- 证据不足、或规则都只是"无法判断" -> 待复核
"""


def load_config(create: bool = True) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update({k: v for k, v in json.loads(CONFIG.read_text(encoding="utf-8")).items()
                        if not k.startswith("_")})
        except Exception as exc:
            print(f"[warn] llm-config.json 读取失败：{exc}")
    elif create:
        CONFIG.parent.mkdir(exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def api_key(cfg: dict) -> str:
    return os.environ.get(cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "") or cfg.get("api_key", "")


def available() -> bool:
    return bool(api_key(load_config()))


def _post(cfg: dict, messages: list[dict], key: str) -> dict:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0),
        "max_tokens": cfg.get("max_tokens", 1200),
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 120)) as resp:
        return json.loads(resp.read().decode())


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    start, depth = t.find("{"), 0
    if start < 0:
        return None
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def rule_brief(kb: dict) -> str:
    """把 W01–W05 整理成提示词里的规则清单。"""
    lines = []
    for rule in kbmod.enabled_work_rules(kb):
        cond = (rule.get("condition") or "").strip()
        note = (rule.get("note") or "").strip()
        lines.append(f"- {rule['id']} {rule.get('title', '')}：{cond}"
                     + (f"（注意：{note}）" if note else ""))
    return "\n".join(lines)


def build_messages(work_text: str, kb: dict) -> list[dict]:
    rules = rule_brief(kb)
    user = (f"知识库版本：{kb.get('version')}\n"
            f"已启用的工作证据规则：\n{rules}\n\n"
            f"候选人工作经历：\n{work_text[:8000]}")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def judge(work_text: str, kb: dict | None = None, cfg: dict | None = None) -> dict:
    """按知识库工作规则取证，返回建议（不直接定论）。"""
    cfg = cfg or load_config()
    kb = kb or kbmod.load()
    key = cfg.get("api_key") or api_key(cfg)
    if not key:
        raise RuntimeError("没有配置 DEEPSEEK_API_KEY")

    messages = build_messages(work_text, kb)
    last = ""
    for attempt in range(int(cfg.get("retries", 2)) + 1):
        try:
            resp = _post(cfg, messages, key)
            text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            data = extract_json(text)
            if data and data.get("final_recommendation"):
                rec = str(data["final_recommendation"]).strip()
                if rec not in ("建议合适", "不合适", "待复核"):
                    rec = "待复核"
                results = []
                for item in (data.get("rule_results") or []):
                    if not isinstance(item, dict):
                        continue
                    results.append({
                        "rule_id": str(item.get("rule_id") or ""),
                        "status": str(item.get("status") or "无法判断"),
                        "evidence": [str((e or {}).get("quote") or "")[:160]
                                     for e in (item.get("evidence") or []) if isinstance(e, dict)],
                        "reason": str(item.get("reason") or "")[:200],
                    })
                return {
                    "recommendation": rec,
                    "rule_results": results,
                    "missing_information": [str(x)[:80] for x in
                                            (data.get("missing_information") or [])][:5],
                    "decision_basis": str(data.get("decision_basis") or "")[:200],
                    "usage": resp.get("usage") or {},
                    "kb_version": kb.get("version", ""),
                }
            last = "输出不是合法 JSON"
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            last = f"HTTP {exc.code}: {body}"
            if exc.code in (401, 403):
                break
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < int(cfg.get("retries", 2)):
            time.sleep(1.5)
    raise RuntimeError(f"判断失败：{last}")


def summarize(result: dict) -> str:
    """一行摘要，便于日志。"""
    hits = [r["rule_id"] for r in result.get("rule_results", [])
            if r["status"] in ("满足", "部分满足")]
    miss = [r["rule_id"] for r in result.get("rule_results", [])
            if r["status"] == "明确不满足"]
    parts = []
    if hits:
        parts.append("满足 " + "/".join(hits))
    if miss:
        parts.append("不满足 " + "/".join(miss))
    return f"{result['recommendation']}（{'；'.join(parts) or '无证据'}）"


def self_test() -> int:
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print("[1] 配置")
    cfg = load_config()
    check("配置文件存在", CONFIG.exists(), str(CONFIG))
    check("默认 deepseek-flash", cfg["model"] == "deepseek-flash", cfg["model"])
    check("base_url", "api.deepseek.com" in cfg["base_url"], cfg["base_url"])

    print("\n[2] JSON 抠取")
    check("裸 JSON", extract_json('{"final_recommendation":"建议合适"}') is not None)
    check("代码块", extract_json('```json\n{"final_recommendation":"不合适"}\n```') is not None)
    check("非法", extract_json("抱歉") is None)

    print("\n[3] 提示词来自知识库")
    kb = kbmod.load()
    brief = rule_brief(kb)
    check("包含 W01", "W01" in brief)
    check("包含 W05", "W05" in brief)
    check("不含被排除规则", "X01" not in brief)
    check("不含待确认规则", "P02" not in brief)

    msgs = build_messages("1) 2024.01-2025.01 某公司 客服", kb)
    check("消息含经历", "某公司 客服" in msgs[-1]["content"])
    check("消息含规则", "W02" in msgs[-1]["content"])

    print(f"\n{ok}/{ok + fail} 通过（真实调用需 DEEPSEEK_API_KEY）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
