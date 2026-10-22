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
{"name":"","sections":[
  {"section":"work","company":"","title":"","period":"","employment":"","responsibilities":[],"tags":[]},
  {"section":"education","school":"","major":"","degree":"","period":""}
]}

规则：
0. name 是候选人的姓名（简历弹窗顶部「张三 刚刚活跃」那种），不要带「在线/刚刚活跃/岁」等字样；
   这一屏看不到姓名就留空字符串。
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
                return {"name": (parsed.get("name") or "").strip(),
                        "sections": parsed.get("sections") or [],
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


NAME_PROMPT = (
    "这是一份简历顶部的姓名区域截图。只输出候选人的姓名（2~6 个汉字或英文名），"
    "不要输出「在线」「刚刚活跃」「3日内活跃」「26岁」「大专」等任何其他文字，"
    "不要标点、不要解释。看不清就输出空字符串。"
)


def read_name(img, band, cfg: dict | None = None, scale: float = 2.5) -> str:
    """用高清裁剪读姓名。

    整屏送模型时姓名只占几十像素，容易认错字（实测「王伟俐」被读成「王伟刚」）；
    把姓名区域单独裁出来放大 2.5 倍再问，准确率高很多，成本几乎不变（图很小）。
    """
    cfg = cfg or load_config()
    key = api_key(cfg)
    if not key:
        return ""

    x0, y0 = max(0, band.x), max(0, band.y)
    x1 = min(img.shape[1], x0 + 480)
    y1 = min(img.shape[0], y0 + 230)      # 姓名那行通常在顶部 180~220px 处
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return ""

    from PIL import Image

    pic = Image.fromarray(crop[:, :, ::-1])
    w, h = pic.size
    pic = pic.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    pic.save(buf, format="PNG", optimize=True)
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    payload = {
        "model": cfg["model"],
        "temperature": 0,
        "max_tokens": 40,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": NAME_PROMPT},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
    }
    try:
        resp = _post(cfg, payload, key)
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return clean_name(text)
    except Exception as exc:
        print(f"  [warn] 高清姓名识别失败：{exc}")
        return ""


STATUS_PATTERNS = re.compile(
    r"(刚刚活跃|今日活跃|本周活跃|本月活跃|\d+\s*日内活跃|\d+\s*小时前活跃|\d+\s*分钟前活跃|"
    r"\d+\s*天前活跃|在线|离线|月内到岗|随时到岗|\d+\s*岁|\d+\s*年(?:经验)?|"
    r"本科|大专|硕士|博士|中专|高中|应届生|离职|在职)"
)


REFUSALS = ("空字符串", "空", "无", "看不清", "无法识别", "none", "null", "n/a")


def clean_name(text: str) -> str:
    """清掉模型/OCR 夹带的活跃状态、年龄、学历等字样。"""
    low = (text or "").strip().strip('"\'').lower()
    if not low or low in REFUSALS or any(low == r for r in REFUSALS):
        return ""
    t = (text or "").strip().strip('"\'“”　.。,，:：')
    t = re.sub(r"[\s　]+", "", t)
    t = STATUS_PATTERNS.sub("", t)
    t = re.sub(r"^[·|\-]+|[·|\-]+$", "", t)
    return t[:12]


def _ask_name(crop, cfg: dict, scale: float = 2.5) -> str:
    from PIL import Image as _Image

    pic = _Image.fromarray(crop[:, :, ::-1])
    w, h = pic.size
    pic = pic.resize((int(w * scale), int(h * scale)), _Image.LANCZOS)
    buf = io.BytesIO()
    pic.save(buf, format="PNG", optimize=True)
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    payload = {
        "model": cfg["model"], "temperature": 0, "max_tokens": 40,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": NAME_PROMPT},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
    }
    try:
        resp = _post(cfg, payload, cfg.get("api_key") or api_key(cfg))
        return clean_name((resp.get("choices") or [{}])[0].get("message", {}).get("content", ""))
    except Exception:
        return ""


def read_name_consensus(img, band, cfg: dict | None = None, log=print) -> tuple[str, list[str]]:
    """读姓名：OCR 定位姓名行 -> 紧贴裁剪放大 -> 多取景投票。

    实测要点：整屏问模型容易认错（王伟俐 -> 王伟刚），宽取景也不行（-> 王佳俐）；
    把姓名那一行**紧贴裁出来**再放大 2.5 倍，多种 padding 下都能稳定读对。
    """
    cfg = cfg or load_config()
    votes: list[tuple[str, int]] = []          # (姓名, 权重)
    raw_all: list[str] = []

    name_box = None
    try:
        import vision as _v
        engine = _v.load_ocr()
        if engine is not None:
            region = _v.Rect(band.x, band.y, 560, 300)
            for it in sorted(_v.ocr_crop(engine, img, region), key=lambda i: i["box"][1]):
                cand = clean_name(it["text"])
                if 2 <= len(cand) <= 6 and not any(ch.isdigit() for ch in cand):
                    name_box = it["box"]
                    votes.append((cand, 1))     # OCR 只给 1 票
                    raw_all.append(cand)
                    break
    except Exception:
        pass

    if name_box is not None:
        x0, y0, x1, y1 = (int(v) for v in name_box)
        crop = img[max(0, y0 - 30): y1 + 30, max(0, x0 - 30): x1 + 30]
        if crop.size:
            tight = _ask_name(crop, cfg, 2.5)
            raw_all.append(tight)
            if tight:
                votes.append((tight, 2))        # 紧贴裁剪权重更高
    else:
        for width, height, scale in [(480, 230, 2.5), (420, 130, 3.2)]:
            crop = img[max(0, band.y): band.y + height, max(0, band.x): band.x + width]
            if crop.size:
                got = _ask_name(crop, cfg, scale)
                raw_all.append(got)
                if got:
                    votes.append((got, 1))

    if not votes:
        return "", raw_all

    score: dict[str, int] = {}
    for name, weight in votes:
        score[name] = score.get(name, 0) + weight
    best = max(score, key=lambda k: score[k])
    # 有分歧就告警，但仍取权重最高者
    if len(score) > 1:
        log(f"  [warn] 姓名候选不一致：{score}，采用 {best}")
    return best, raw_all

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

    print("\n[1b] 姓名抽取与清洗")
    for raw, want in [("王伟俐", "王伟俐"), ("王伟俐 3日内活跃", "王伟俐"),
                      ("张锦敏刚刚活跃", "张锦敏"), ("李四 26岁 大专", "李四"),
                      ('"王五"', "王五")]:
        got = clean_name(raw)
        check(f"清洗 {raw!r}", got == want, f"得到 {got!r}")
    check("能从模型输出取 name",
          (extract_json('{"name":"吴欣烨","sections":[]}') or {}).get("name") == "吴欣烨")

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
