"""用多模态大模型读简历截图，替代 OCR。

为什么换掉 OCR：
    OCR 逐行识别再拼版式，遇到「公司/职位/时间分期、章节标题与正文同行、跨屏重叠」
    就必然出错（错字、串行、漏行）。视觉模型直接"看图理解版式"，一次给出结构化条目。

支持的端点（都是 OpenAI 兼容的 /chat/completions）：
    阿里通义千问   https://dashscope.aliyuncs.com/compatible-mode/v1   qwen-vl-max
    智谱 GLM      https://open.bigmodel.cn/api/paas/v4                 glm-4v-plus
    字节豆包      https://ark.cn-beijing.volces.com/api/v3             doubao-vision-pro-32k
    Kimi          https://api.moonshot.cn/v1                          moonshot-v1-8k-vision-preview

配置：data\\vlm-config.json（首次运行自动生成模板）
密钥：默认从环境变量 VLM_API_KEY 读取，也可以直接写在配置文件里（不推荐入库）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "vlm-config.json"

DEFAULT_CONFIG = {
    "_说明": "视觉模型读取简历。把 api_key 放进环境变量 VLM_API_KEY，或填在这里（不要提交到仓库）。",
    "provider": "qwen",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen-vl-max",
    "api_key_env": "VLM_API_KEY",
    "api_key": "",
    "temperature": 0,
    "max_tokens": 2000,
    "timeout": 90,
    "retries": 2,
    "max_image_side": 1600,
}

PROMPT = """你是简历信息抽取器。图片是 BOSS 直聘「线上简历」弹窗的一屏截图（可能只截到某段经历的一部分）。

请只输出 JSON，不要解释、不要 markdown 代码块。结构：
{"sections":[
  {"section":"work","company":"","title":"","period":"","employment":"","responsibilities":[],"tags":[]},
  {"section":"education","school":"","major":"","degree":"","period":""}
]}

规则：
1. section 只能是 work / education / project / campus / other。
2. work = 工作经历（含实习）；project = 项目经历/项目经验；campus = 校园经历/社团经历/在校经历；education = 教育经历。
3. company 只填单位名称；职位放 title；时间放 period（原样抄，如「2025.03-至今」）。
   **不要把职责、业务描述、标签写进 company。** 单位名看不清就留空。
4. 同一条经历的多行职责合并进 responsibilities（保持原文，不要改写、不要总结）。
5. 灰色小标签（如「电商运营」「数据分析」）放 tags。
6. 教育经历里：学校放 school，专业放 major，学历放 degree（本科/大专/硕士…），时间放 period。
7. 文字被截断（一行只有半句）时照抄，不要补全。
8. 这一屏看不到的单位名/字段，留空即可，不要猜测。
9. 如果这一屏里某条经历只有下半部分（职责），也照样输出该条目，company 留空。
"""


# ---------------------------------------------------------------- 配置

def load_config(create: bool = True) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update({k: v for k, v in json.loads(CONFIG.read_text(encoding="utf-8")).items()
                        if not k.startswith("_")})
        except Exception as exc:
            print(f"[warn] vlm-config.json 读取失败：{exc}")
    elif create:
        CONFIG.parent.mkdir(exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def api_key(cfg: dict) -> str:
    return os.environ.get(cfg.get("api_key_env", "VLM_API_KEY"), "") or cfg.get("api_key", "")


def available() -> bool:
    """有没有可用的 key（没 key 就回退 OCR）。"""
    return bool(api_key(load_config()))


# ---------------------------------------------------------------- 图像

def encode_image(bgr, max_side: int = 1600) -> str:
    """numpy BGR -> PNG base64 data URI（超长边会等比缩小）。"""
    from PIL import Image

    img = Image.fromarray(bgr[:, :, ::-1])
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- 调用

def _post(cfg: dict, payload: dict, key: str) -> dict:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 90)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON（容忍 ```json 包裹和前后废话）。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
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


def read_screen(bgr, cfg: dict | None = None, log=print) -> dict:
    """读一屏截图，返回 {"sections": [...], "raw": "原始输出", "usage": {...}}。"""
    cfg = cfg or load_config()
    key = api_key(cfg)
    if not key:
        raise RuntimeError("没有配置视觉模型 API key（设环境变量 VLM_API_KEY）")

    data_uri = encode_image(bgr, cfg.get("max_image_side", 1600))
    payload = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0),
        "max_tokens": cfg.get("max_tokens", 2000),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }

    last_error = ""
    for attempt in range(int(cfg.get("retries", 2)) + 1):
        try:
            resp = _post(cfg, payload, key)
            text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if isinstance(text, list):          # 少数端点返回分段
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            parsed = extract_json(text)
            if parsed is not None:
                return {"sections": parsed.get("sections") or [],
                        "raw": text, "usage": resp.get("usage") or {}}
            last_error = "模型输出不是合法 JSON"
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            last_error = f"HTTP {exc.code}: {body}"
            if exc.code in (401, 403):
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < int(cfg.get("retries", 2)):
            time.sleep(1.5)
    raise RuntimeError(f"视觉模型读取失败：{last_error}")


# ---------------------------------------------------------------- 结构归一

def normalize(sections) -> dict:
    """把模型输出整理成内部结构：work / education / projects / campus。

    容忍模型返回 {"sections": [...]} 或直接 [...]。
    """
    if isinstance(sections, dict):
        sections = sections.get("sections") or []
    work, education, projects, campus = [], [], [], []
    for item in sections or []:
        kind = (item.get("section") or "").strip().lower()
        if kind in ("education", "edu"):
            if not any(item.get(k) for k in ("school", "major", "degree", "period")):
                continue
            # 过滤假教育条目：没有校名、时间又不是年份区间的（多是头部「22岁|本科|应届生」被误判）
            school = (item.get("school") or "").strip()
            period = (item.get("period") or "").strip()
            year_range = bool(__import__("re").search(r"^\s*\d{4}\s*[-–~至]\s*\d{4}\s*$", period))
            if not school and not year_range:
                continue
            education.append({
                "school": (item.get("school") or "").strip(),
                "major": (item.get("major") or "").strip(),
                "degree": (item.get("degree") or "").strip(),
                "period": (item.get("period") or "").strip(),
                "source": "vlm",
            })
            continue

        entry = {
            "company": (item.get("company") or "").strip(),
            "title": (item.get("title") or "").strip(),
            "period": (item.get("period") or "").strip(),
            "employment": (item.get("employment") or "").strip(),
            "descriptions": [str(x).strip() for x in (item.get("responsibilities") or []) if str(x).strip()],
            "tags": [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()],
            "source": "vlm",
        }
        if not (entry["company"] or entry["title"] or entry["period"] or entry["descriptions"]):
            continue
        # 碎片：没有公司也没有时间，只有零散职责 -> 多半是上一屏的续行，丢掉
        if not entry["company"] and not entry["period"]:
            continue
        # 公司名其实是职位（没有机构后缀、含职位词）-> 归到 title
        if entry["company"] and not any(w in entry["company"] for w in ("公司", "集团", "工作室",
                                                                        "事务所", "店", "厂", "中心")):
            if any(w in entry["company"] for w in ("运营", "客服", "销售", "助理", "专员", "教师",
                                                   "摄影", "剪辑", "设计", "主管", "经理", "实习")):
                entry["title"] = (entry["company"] + " " + entry["title"]).strip()
                entry["company"] = ""
        {"project": projects, "campus": campus}.get(kind, work).append(entry)

    return {"work": work, "education": education, "projects": projects, "campus": campus}


def merge_screens(collected: list[dict], new_entries: list[dict]) -> list[dict]:
    """跨屏合并同一条经历：公司名相似（或一侧为空）且时间一致就合并，补缺失字段、并职责。"""
    import difflib

    def sim(a: str, b: str) -> float:
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b:
            return 0.0
        if a.startswith(b) or b.startswith(a):
            return 1.0
        return difflib.SequenceMatcher(None, a, b).ratio()

    def norm(p: str) -> str:
        return re.sub(r"[\s　]", "", p or "")

    for entry in new_entries:
        target = None
        for old in collected:
            same_period = norm(old.get("period", "")) == norm(entry.get("period", ""))
            name_old, name_new = old.get("company", ""), entry.get("company", "")
            name_ok = sim(name_old, name_new) >= 0.8 or not name_old or not name_new
            if same_period and name_ok:
                target = old
                break
        if target is None:
            collected.append(entry)
            continue
        for field in ("company", "title", "period", "employment"):
            if not target.get(field) and entry.get(field):
                target[field] = entry[field]
        for field in ("descriptions", "tags"):
            for text in entry.get(field) or []:
                if text and text not in target.setdefault(field, []):
                    target[field].append(text)
    return collected


def self_test() -> int:
    """不需要 key 的自检：配置、JSON 抠取、归一、合并。"""
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
    check("配置文件已生成", CONFIG.exists(), str(CONFIG))
    check("默认 base_url 指向通义", "dashscope" in cfg["base_url"], cfg["base_url"])
    check("key 状态可判断", isinstance(available(), bool), f"available={available()}")

    print("\n[2] JSON 抠取")
    check("裸 JSON", extract_json('{"sections":[]}') is not None)
    check("代码块包裹", extract_json('```json\n{"sections":[{"section":"work"}]}\n```') is not None)
    check("前后有废话",
          extract_json('好的，结果如下：{"sections":[{"section":"work"}]} 以上') is not None)
    check("非法输出返回 None", extract_json("抱歉我无法识别") is None)

    print("\n[3] 结构归一")
    raw = {"sections": [
        {"section": "work", "company": "福州某公司", "title": "运营", "period": "2024.01-至今",
         "responsibilities": ["负责店铺运营"], "tags": ["电商"]},
        {"section": "education", "school": "厦门大学", "major": "广告", "degree": "本科",
         "period": "2019-2023"},
        {"section": "project", "company": "", "title": "某项目", "period": "2023.01-2023.05"},
        {"section": "campus", "title": "学生会", "period": "2020-2021"},
        {"section": "work", "company": "", "title": "", "period": "", "responsibilities": []},
    ]}
    out = normalize(raw["sections"])
    check("工作经历 1 条（空条目被丢）", len(out["work"]) == 1, str(len(out["work"])))
    check("教育 1 条", len(out["education"]) == 1 and out["education"][0]["school"] == "厦门大学")
    check("项目单独归类", len(out["projects"]) == 1, str(len(out["projects"])))
    check("校园单独归类", len(out["campus"]) == 1, str(len(out["campus"])))

    print("\n[4] 跨屏合并")
    a = [{"company": "", "title": "运营", "period": "2024.01-2024.06",
          "descriptions": ["做A"], "tags": []}]
    b = [{"company": "福州某公司", "title": "", "period": "2024.01-2024.06",
          "descriptions": ["做A", "做B"], "tags": []}]
    merged = merge_screens(a, b)
    check("同时间段合并成 1 条", len(merged) == 1, str(len(merged)))
    check("补回公司名", merged[0]["company"] == "福州某公司", merged[0]["company"])
    check("职责合并去重", merged[0]["descriptions"] == ["做A", "做B"], str(merged[0]["descriptions"]))

    c = [{"company": "另一家公司", "title": "运营", "period": "2024.01-2024.06",
          "descriptions": [], "tags": []}]
    merged2 = merge_screens(merged, c)
    check("不同公司不合并", len(merged2) == 2, str([m["company"] for m in merged2]))

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
