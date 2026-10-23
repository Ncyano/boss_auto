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
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key_env": "DEEPSEEK_API_KEY",
    "api_key": "",
    "temperature": 0,
    "max_tokens": 2500,
    "timeout": 120,
    "retries": 3,
    "thinking": "disabled",
}


def load_config(create: bool = True) -> dict:
    """判断模型配置：统一从 config.json 的 judge 段读取。"""
    try:
        import settings
        return dict(settings.section("judge"))
    except Exception as exc:
        print(f"[warn] 读取统一配置失败，使用内置默认值：{exc}")
        return dict(DEFAULT_CONFIG)


def api_key(cfg: dict | None = None) -> str:
    try:
        import settings
        return settings.api_key("judge")
    except Exception:
        cfg = cfg or load_config()
        return os.environ.get(cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "") or cfg.get("api_key", "")


def available() -> bool:
    return bool(api_key())


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
7. 尽量简短：每条证据引用不超过 30 字，reason 可留空。

输出格式：
{
  "rule_results": [
    {"rule_id": "W01", "status": "满足|部分满足|明确不满足|无法判断",
     "evidence": [{"quote": "原文片段（≤30字）", "experience_index": 1}],
     "reason": ""}
  ],
  "missing_information": ["缺少什么信息"],
  "final_recommendation": "建议合适|不合适",
  "decision_basis": "一句话总结依据"
}

判定口径（按人事的真实习惯）：
- 有任意一条 W01–W03 为"满足"或"部分满足"（职责里出现客户沟通/客服售后/销售导购的具体动作）
  -> 建议合适。
- 全部经历的职责都属于：生产/仓库/文员/内勤/行政后勤/纯技术/纯设计/教育教务/餐饮后厨，
  没有任何对客户沟通或线上运营的动作 -> 不合适（这就是 W05 要求的"职责层面的不满足"，
  证据就是这些职责原文本身）。
- 电商店铺运营（选品、上架、SKU维护、活动报名、推广投放、数据复盘、供应链对接）
  属于 W04 运营相关经验，可判"建议合适"，不要因为"没有直接对客户沟通"就判不合适。
- 只输出两个结论：建议合适 / 不合适，不允许输出"待复核"。
- 资料不足（没有职责原文、经历截断、只有职位名）时，按**现有证据**判断：
  看不到任何客户沟通/运营动作 -> 不合适；有明确相关动作 -> 建议合适。
- 不要因为"职位名不典型"或"写得不详细"就回避判断。
"""




def _post(cfg: dict, messages: list[dict], key: str) -> dict:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0),
        "max_tokens": cfg.get("max_tokens", 2500),
        "response_format": {"type": "json_object"},
        # deepseek 默认开启思考模式；分类任务不需要思维链，关掉更快更稳（且思考模式下 temperature 不生效）
        "thinking": {"type": str(cfg.get("thinking", "disabled"))},
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
    """把 W01–W05 与画像清单整理成提示词里的规则部分。"""
    lines = []
    for rule in kbmod.enabled_work_rules(kb):
        cond = (rule.get("condition") or "").strip()
        note = (rule.get("note") or "").strip()
        lines.append(f"- {rule['id']} {rule.get('title', '')}：{cond}"
                     + (f"（注意：{note}）" if note else ""))

    params = kbmod.params_of(kb, "W07") or {}
    prefer = params.get("prefer_titles") or []
    exclude = (kbmod.params_of(kb, "NP01") or {}).get("exclude_titles") or []
    if prefer:
        lines.append("\n【优先考虑岗位】（命中即强正向，可建议合适）：\n"
                     + "、".join(prefer))
    weak = (kbmod.params_of(kb, "W07") or {}).get("exclude_titles") or []
    if weak:
        lines.append("\n【不算运营岗】（纯销售/门店店员/理财保险/课程顾问/电销/餐饮服务；"
                     "单独出现时不作为 W04/W07 命中依据）：\n" + "、".join(weak))

    if exclude:
        lines.append("\n【不考虑岗位】（只做技术/职能/生产，无对客户沟通）：\n"
                     + "、".join(exclude)
                     + "\n注意：只要有一段经历属于『优先考虑』清单，就不因其它经历是排除岗位而否定。")
    return "\n".join(lines)


FEWSHOT_FILE = ROOT / "data" / "fewshot.json"


def load_fewshot(limit: int | None = None) -> list[dict]:
    """读取训练集产出的少样本示例（data/fewshot.json）。"""
    if not FEWSHOT_FILE.exists():
        return []
    try:
        data = json.loads(FEWSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    examples = [e for e in (data.get("examples") or [])
                if e.get("user") and e.get("assistant")]
    return examples[:limit] if limit else examples


def build_messages(work_text: str, kb: dict, shots: list[dict] | None = None) -> list[dict]:
    rules = rule_brief(kb)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ex in (shots if shots is not None else load_fewshot()):
        messages.append({"role": "user", "content": ex["user"]})
        messages.append({"role": "assistant", "content": ex["assistant"]})
    user = (f"知识库版本：{kb.get('version')}\n"
            f"已启用的工作证据规则：\n{rules}\n\n"
            f"候选人工作经历：\n{work_text[:8000]}")
    messages.append({"role": "user", "content": user})
    return messages


def judge(work_text: str, kb: dict | None = None, cfg: dict | None = None,
          shots: list[dict] | None = None) -> dict:
    """按知识库工作规则取证，返回建议（不直接定论）。"""
    cfg = cfg or load_config()
    kb = kb or kbmod.load()
    key = cfg.get("api_key") or api_key(cfg)
    if not key:
        raise RuntimeError("没有配置 DEEPSEEK_API_KEY")

    messages = build_messages(work_text, kb, shots)
    last = ""
    for attempt in range(int(cfg.get("retries", 2)) + 1):
        try:
            resp = _post(cfg, messages, key)
            text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            data = extract_json(text)
            if data and data.get("final_recommendation"):
                rec = str(data["final_recommendation"]).strip()
                if rec not in ("建议合适", "不合适"):
                    # 模型偶尔仍会输出待复核/其他值，一律按不合适收口（二元口径）
                    rec = "不合适" if ("不合适" in rec or "待" in rec) else "建议合适"
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


# ---------------------------------------------------------------- 文本结构化

STRUCTURE_PROMPT = """你是简历解析器。输入是一份简历截图 OCR 出来的纯文本（可能有排版错乱、字符残缺）。
请把它解析成 JSON，**只输出 JSON**，不要解释。

格式：
{
  "name": "姓名（顶部那个名字，没有就空）",
  "basic": {"age": "数字", "education_mode": "全日制|非全日制|", "graduation_year": "四位年份", "status": "求职状态原文"},
  "sections": [
    {"section": "work", "company": "公司名", "title": "职位", "period": "2022.03-2025.11",
     "responsibilities": ["职责原文，尽量完整"], "tags": []},
    {"section": "education", "school": "学校", "major": "专业", "degree": "学历", "period": "2019-2023"}
  ]
}

规则：
1. section 只能是 work / education / project / campus / other
2. company 只能是公司名，绝不能把职责、标签、行业写进去
3. 职责按原文抄，不要改写、不要总结；截断的照原样保留
4. OCR 里出现的"工作内容/工作描述"后面的条目都算该段经历的职责
5. 区块归属必须严格按简历本身的小标题走：
   「工作经历」→ work；「项目经历/项目经验」→ project；
   「校园经历/在校经历/社团经历/学生会」→ campus；
   「志愿经历/志愿服务/公益活动」→ other；「教育经历」→ education
6. 「XX大赛」「挑战赛」「策划赛」「创新创业」「竞赛」「网上购物系统」这类项目名一律 project，
   即使它们出现在工作经历区块附近也不能放进 work
7. 某段工作没写公司名时，把该行开头那段文字原样填进 company（例如「跨境电商运营」「自媒体」），
   **不要留空、也不要编造公司全称**；岗位写进 title
8. 同一段经历只输出一次，不要因为跨屏重复而重复输出
9. 不要编造文本里没有的内容
"""


def structure_text(text: str, cfg: dict | None = None, kb: dict | None = None) -> dict:
    """把 OCR 出来的纯文本解析成结构化简历（用文本模型，约 1~2 秒）。"""
    cfg = cfg or load_config()
    key = cfg.get("api_key") or api_key(cfg)
    if not key:
        raise RuntimeError("没有配置 DEEPSEEK_API_KEY")

    # 结构化输出较长，单独给足 token（原配置会被截断成非法 JSON）
    local_cfg = dict(cfg)
    local_cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 0), 2500)
    messages = [{"role": "system", "content": STRUCTURE_PROMPT},
                {"role": "user", "content": text[:8000]}]
    last = ""
    for attempt in range(int(cfg.get("retries", 2)) + 1):
        try:
            resp = _post(local_cfg, messages, key)
            raw = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            data = extract_json(raw)
            if data and (data.get("sections") is not None or data.get("name")):
                return {"data": data, "usage": resp.get("usage") or {}}
            last = "输出不是合法 JSON"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < int(cfg.get("retries", 2)):
            import time as _t
            _t.sleep(1.0)
    raise RuntimeError(f"结构化失败：{last}")

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
    import settings as _settings
    check("统一配置文件存在", _settings.config_path().exists(), str(_settings.config_path()))
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
