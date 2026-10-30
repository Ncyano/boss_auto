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
    "provider": "qwen",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3-vl-plus",
    "api_key_env": "VLM_API_KEY",
    "api_key": "",
    "temperature": 0,
    "max_tokens": 2000,
    "timeout": 120,
    "retries": 2,
    "max_image_side": 1024,
}


def load_config(create: bool = True) -> dict:
    """视觉模型配置：统一从 config.json 的 vision 段读取。"""
    try:
        import settings
        return dict(settings.section("vision"))
    except Exception as exc:
        print(f"[warn] 读取统一配置失败，使用内置默认值：{exc}")
        return dict(DEFAULT_CONFIG)


def api_key(cfg: dict | None = None) -> str:
    # 调用方显式传了 key（比如拿另一种 provider 做对比）就优先用它
    if cfg and cfg.get("api_key"):
        return str(cfg["api_key"])
    cfg = cfg or load_config()
    try:
        import settings
        return settings.api_key("vision")
    except Exception:
        return os.environ.get(cfg.get("api_key_env", "VLM_API_KEY"), "") or cfg.get("api_key", "")


def available() -> bool:
    return bool(api_key())


PROMPT = """你是简历信息抽取器。图片是 BOSS 直聘「线上简历」弹窗的一屏截图（可能只截到某段经历的一部分）。

请只输出 JSON，不要解释、不要 markdown 代码块。结构：
{"name":"","basic":{"age":"","education_mode":"","graduation_year":"","status":""},"sections":[
  {"section":"work","company":"","title":"","period":"","employment":"","responsibilities":[],"tags":[]},
  {"section":"education","school":"","major":"","degree":"","period":""}
]}

规则：
0a. basic 是简历顶部那行基本信息：
    age=年龄数字（如「26岁」就填 26，没有就空）
    education_mode=学习形式，只能是 全日制 / 非全日制 / 空（原文写了「非全日制」「函授」「自考」「成人」就填非全日制）
    graduation_year=毕业年份（四位数字，如 2026；没写就空）
    status=求职状态原样（如「离职-随时到岗」「在职-月内到岗」「应届生」）
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


# ---------------------------------------------------------------- 配置（见 settings.py）

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
                        "basic": parsed.get("basic") or {},
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

def normalize_basic(raw: dict) -> dict:
    """整理简历顶部基本信息（年龄/学习形式/毕业年份/求职状态）。"""
    if isinstance(raw, dict) and isinstance(raw.get("basic"), dict):
        basic = raw["basic"]            # read_screen 的返回：{"name","basic","sections",...}
    elif isinstance(raw, dict):
        basic = raw                     # 直接传 {"age":...}
    else:
        basic = {}
    out = {"age": None, "education_mode": "", "graduation_year": None, "status": ""}
    age = str(basic.get("age") or "").strip()
    if age.isdigit() and 15 <= int(age) <= 70:
        out["age"] = int(age)
    mode = str(basic.get("education_mode") or "").strip()
    if "非全日制" in mode or any(k in mode for k in ("函授", "自考", "成人", "业余", "网络教育")):
        out["education_mode"] = "非全日制"
    elif "全日制" in mode:
        out["education_mode"] = "全日制"
    year = str(basic.get("graduation_year") or "").strip()
    if year.isdigit() and 1990 <= int(year) <= 2035:
        out["graduation_year"] = int(year)
    out["status"] = str(basic.get("status") or "").strip()[:20]
    return out


def normalize(sections) -> dict:
    """把模型输出整理成内部结构：work / education / projects / campus。

    容忍模型返回 {"sections": [...]} 或直接 [...]。
    """
    if isinstance(sections, dict):
        sections = sections.get("sections") or []
    work, education, projects, campus, fragments = [], [], [], [], []
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
        # 没有公司也没有时间、只有职责 -> 是跨屏续行片段，单独返回交给上层归属（不能直接丢）
        if not entry["company"] and not entry["period"]:
            fragments.append({**entry, "section": kind or "work"})
            continue
        # 公司名其实是职位（没有机构后缀、含职位词）-> 归到 title
        if entry["company"] and not any(w in entry["company"] for w in ("公司", "集团", "工作室",
                                                                        "事务所", "店", "厂", "中心")):
            if any(w in entry["company"] for w in ("运营", "客服", "销售", "助理", "专员", "教师",
                                                   "摄影", "剪辑", "设计", "主管", "经理", "实习")):
                entry["title"] = (entry["company"] + " " + entry["title"]).strip()
                entry["company"] = ""
        {"project": projects, "campus": campus}.get(kind, work).append(entry)

    return {"work": work, "education": education, "projects": projects,
            "campus": campus, "fragments": fragments}


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


# 姓名合法性：2~6 个汉字，或 2~20 个英文字母（BOSS 上有英文昵称）
HANZI = re.compile(r"^[\u4e00-\u9fa5]{2,6}$")
LATIN = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,19}$")
NON_NAME_WORDS = {
    "在线", "离职", "在职", "应届", "本科", "大专", "硕士", "博士", "中专", "高中",
    "离校", "到岗", "活跃", "暂无", "空字符串", "无", "看不清", "姓名", "候选人",
    # 简历里的区块标题/常见词，OCR 常常把它们当姓名读出来
    "工作经历", "教育经历", "项目经历", "校园经历", "实习经历", "求职期望", "期望职位",
    "自我评价", "资格证书", "专业技能", "荣誉奖项", "基本信息", "求职意向",
    "价格", "负责", "内容", "业绩", "描述", "职责", "标签", "更多", "查看", "全部",
    "考虑机会", "随时到岗", "月内到岗", "看看机会", "求职期望", "期望职位",
    "牛人", "荐牛人", "推荐牛人", "牛人分析器", "在线", "刚刚活跃", "今日活跃", "新牛人",
    "离职", "在职", "应届生", "在校",
}

# 职位/标签类词：这些出现在"姓名"里说明抓到的是简历标签或职位，不是人名
TAG_WORDS = {
    "优化", "投放", "成本", "运营", "客服", "销售", "专员", "经理", "主管", "助理",
    "总监", "编辑", "剪辑", "设计", "开发", "工程", "管理", "推广", "采购", "财务",
    "人事", "行政", "仓储", "物流", "外贸", "文员", "主播", "导购", "实习", "兼职",
    "数据分析", "新媒体", "电商", "跨境", "店铺", "账号", "视频", "直播", "标签",
}


def is_plausible_name(text: str) -> bool:
    """判断是不是一个像样的姓名。

    实测会把页面上的杂字、图标文字当成姓名（/UR,±>、丨 | 离校、主> 等），
    这些必须拒掉，宁可标"姓名未识别"也不写脏数据。
    """
    import re as _re
    t = (text or "").strip().rstrip("网直聘 ")
    if not t or t in NON_NAME_WORDS:
        return False
    # 备注：英文马甲（含 CRTCRT 这种全大写）是真人自取的昵称，不能当乱码拒掉
    if any(w in t for w in TAG_WORDS):
        return False
    if HANZI.match(t):
        # 中文姓名一般 2~4 字；5~6 字要额外确认不含标签词（上面已排除）
        return True
    if LATIN.match(t):
        return True
    return False

def clean_name(text: str) -> str:
    """清掉模型/OCR 夹带的活跃状态、年龄、学历等字样。"""
    low = (text or "").strip().strip('"\'').lower()
    if not low or low in REFUSALS or any(low == r for r in REFUSALS):
        return ""
    t = (text or "").strip().strip('"\'“”　.。,，:：')
    t = re.sub(r"[\s　]+", "", t)
    t = STATUS_PATTERNS.sub("", t)
    t = re.sub(r"^[·|\-]+|[·|\-]+$", "", t)
    t = t[:12]
    return t if is_plausible_name(t) else ""


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

# ---------------------------------------------------------------- OCR 模式（快）

# 注意：多写"逐行/坐标/JSON"这类词会让 OCR 模型改吐坐标框，保持这句原文最稳
OCR_PROMPT = "读取图中所有文字，按阅读顺序输出，不要解释、不要总结。"
OCR_DEFAULT_MODEL = "qwen-vl-ocr"


def ocr_model(cfg: dict | None = None) -> str:
    """OCR 用的模型名。

    qwen 走专用 OCR 模型（qwen-vl-ocr）；
    其他 provider（如 deepseek）没有单独的 OCR 模型，直接用 cfg["model"]。
    """
    cfg = cfg or load_config()
    if str(cfg.get("provider") or "qwen").lower() != "qwen":
        return cfg.get("model") or OCR_DEFAULT_MODEL
    return cfg.get("ocr_model") or OCR_DEFAULT_MODEL


def read_screen_text(crop, cfg: dict | None = None, log=print) -> dict:
    """用专用 OCR 模型读一屏，返回纯文本（比通用视觉模型快一倍）。"""
    cfg = cfg or load_config()
    key = api_key(cfg)
    if not key:
        raise RuntimeError("没有配置视觉模型 API key")

    data_uri = encode_image(crop, int(cfg.get("max_image_side", 1024)))
    payload = {
        "model": ocr_model(cfg),
        "temperature": 0,
        "max_tokens": int(cfg.get("ocr_max_tokens", 2000)),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
    }

    last = ""
    for attempt in range(int(cfg.get("retries", 2)) + 1):
        try:
            resp = _post(cfg, payload, key)
            text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if text and text.strip():
                return {"text": text.strip(), "usage": resp.get("usage") or {},
                        "model": ocr_model(cfg)}
            last = "输出为空"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < int(cfg.get("retries", 2)):
            import time as _t
            _t.sleep(1.0)
    raise RuntimeError(f"OCR 读取失败：{last}")

def name_consensus_detail(img, band, cfg: dict | None = None, log=print) -> dict:
    """读姓名并返回候选明细：{"name":..., "candidates":[{value, source, weight}], "disputed":bool}"""
    name, votes = read_name_consensus(img, band, cfg, log=log)
    uniq = [v for v in dict.fromkeys(votes) if v]
    return {
        "name": name,
        "candidates": [{"value": v} for v in uniq],
        "votes": votes,
        "disputed": len(uniq) > 1,
    }

NON_FULLTIME_KEYS = ("非全日制", "函授", "自考", "成人教育", "业余", "网络教育", "国开", "开放大学")


def scan_education_mode(img) -> str:
    """用 OCR 在截图里直接找「非全日制」这类字样。

    视觉模型会把「本科 · 非全日制」读成「全日制」（实测），而这条一旦判错，
    人事规则会直接放行不合格的人。所以用关键词做一次硬校验，命中就以 OCR 为准。
    """
    try:
        import vision as _v
        engine = _v.load_ocr()
        if engine is None:
            return ""
        items = _v.ocr_crop(engine, img, _v.Rect(0, 0, img.shape[1], img.shape[0]))
        text = " ".join((it.get("text") or "") for it in items).replace(" ", "")
        for key in NON_FULLTIME_KEYS:
            if key in text:
                return "非全日制"
        # 明确写了全日制/统招的，也要认出来（否则会被当成"未知"）
        for key in ("全日制", "统招", "普通高校全日制", "统招全日制"):
            if key in text:
                return "全日制"
        return ""
    except Exception:
        return ""


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
    import settings as _settings
    check("统一配置文件存在", _settings.config_path().exists(), str(_settings.config_path()))
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

def ask_vision(prompt: str, bgr, cfg: dict | None = None, log=print) -> dict:
    """视觉大模型直读：给图片 + 提示词，要求返回 JSON。

    与 read_screen_text 的区别：
        read_screen_text → 只出文字（再交给结构化模型）
        ask_vision       → 一步出结构化 JSON（省一次调用，且能用提示词约束"只看哪块"）

    返回 {"data": {...}, "usage": {...}}；失败时 data 为空字典。
    """
    cfg = cfg or load_config()
    if not api_key(cfg):
        return {"data": {}, "usage": {}}
    max_side = int(cfg.get("max_image_side") or 1400)
    data_uri = encode_image(bgr, max_side)
    payload = {
        "model": cfg.get("model") or "qwen3-vl-plus",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0,
        "max_tokens": int(cfg.get("max_tokens") or 2000),
    }
    try:
        res = _post(cfg, payload, api_key(cfg))
    except Exception as exc:
        log(f"[warn] 视觉直读失败：{exc}")
        return {"data": {}, "usage": {}}
    usage = res.get("usage") or {}
    text = ""
    try:
        text = res["choices"][0]["message"]["content"]
    except Exception:
        pass
    obj = extract_json(text) or {}
    if isinstance(obj, dict) and "data" in obj and isinstance(obj["data"], dict):
        obj = obj["data"]
    return {"data": obj if isinstance(obj, dict) else {}, "usage": usage}
