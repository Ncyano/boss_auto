"""在简历弹窗内部滚动，把整份简历（尤其教育经历）读完。

三个要点
--------
1. **滚在弹窗里**，不是滚整个页面：滚轮指针必须落在弹窗正文区域上。
2. **逐屏读 + 去重拼接**：每滚一次识别一屏，只把没见过的行追加进去。
3. **滚动本身也要过输入闸门**：`winenv.scroll()` 会先确认光标到位，不到位直接抛
   `InputBlocked`，不会在别处乱滚。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

import vision
import winenv

# 弹窗正文相对「打招呼」大按钮的偏移（实测：按钮 x=1609 时正文区 800..1574）
PANEL_LEFT_OFFSET = 809      # 按钮左缘 - 809 = 正文左边界
PANEL_RIGHT_OFFSET = 35      # 正文右边界 = 按钮左缘 - 35
PANEL_TOP_RATIO = 0.04       # 相对窗口高度，正文上边界
PANEL_BOTTOM_RATIO = 0.94    # 相对窗口高度，正文下边界

SECTION_HEADS = ("工作经历", "教育经历", "项目经历", "自我评价", "求职意向")

# 姓名行常见形态：`颜阔森刚刚活跃` / `小悦 在线` / `张敏敏 刚刚活跃`
STATUS_WORDS = ("刚刚活跃", "在线", "今日活跃", "本周活跃", "月内活跃", "刚刚看过", "今日看过",
                "本周看过", "离职-随时到岗", "在职-月内到岗", "在职-暂不考虑", "随时到岗",
                "离职", "在职", "已实名认证")
NAME_JUNK = ("招聘规", "推荐牛人", "我的客服", "面试", "招聘数据", "账号权益",
             "换一批", "查看更多", "查看全部", "继续沟通", "牛人分析器")
NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


def guess_name_from_text(text: str) -> str:
    """从简历弹窗的纯文本前几行里认姓名（比按坐标更稳，因为能看上下文）。

    规律：姓名下面一两行必然出现「刚刚活跃/在线/…」或「27岁丨大专…」。
    所以：候选行去掉状态词后剩 2~6 个汉字，且往后两行里出现状态词或年龄 → 判为姓名。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:6]
    for i, line in enumerate(lines):
        compact = line.replace(" ", "")
        if any(j in compact for j in NAME_JUNK):
            continue
        cleaned = compact
        for word in STATUS_WORDS:
            cleaned = cleaned.replace(word, "")
        cleaned = cleaned.strip(" 丨|·-、")
        if not re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", cleaned):
            continue
        nxt = "".join(lines[i + 1:i + 3])
        status_next = any(w in nxt for w in ("活跃", "在线", "到岗", "离职", "在职"))
        age_next = "岁" in nxt
        if status_next or age_next:
            return cleaned
    return ""


def guess_name(lines: list[dict]) -> str:
    """从简历弹窗顶部的 OCR 行里猜出候选人姓名。

    优先找带「在线 / 刚刚活跃」等状态词的那一行（去掉状态词后剩 2~4 个汉字）；
    否则找独立短行，且下一行是「26岁丨大专…」这种年龄行。
    """
    top = sorted(lines, key=lambda d: d["box"][1])[:10]
    for it in top:
        t = it["text"].strip()
        if any(j in t for j in NAME_JUNK) or not any(s in t for s in STATUS_WORDS):
            continue
        for s in STATUS_WORDS:
            t = t.replace(s, "")
        t = t.strip(" 丨|·-")
        if NAME_RE.match(t):
            return t
    for i, it in enumerate(top):
        t = it["text"].strip()
        if not NAME_RE.match(t) or any(j in t for j in NAME_JUNK):
            continue
        if any(s in t for s in STATUS_WORDS):      # 「今日活跃」这种状态行不是姓名
            continue
        # 姓名下一两行通常是状态词 + 「21岁丨大专…」，所以往后看两行再判定
        nxt = " ".join(x["text"] for x in top[i + 1:i + 3])
        if "岁" in nxt or "年" in nxt:
            return t
    return ""


@dataclass
class ReadResult:
    lines: list[dict] = field(default_factory=list)
    text: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    steps: int = 0
    reached_education: bool = False
    stopped_reason: str = ""
    name: str = ""
    work: list = field(default_factory=list)        # [{company,title,period,...}]
    education: list = field(default_factory=list)   # [{school,major,degree,period}]
    sidebar: list = field(default_factory=list)     # 侧栏「经历概览」解析结果
    incomplete: bool = False                        # 没读到教育经历/被截断 -> 需复核
    complete: bool = False                          # 读取完成（可进入筛选）
    at_top: bool = False                            # 已确认滚到顶部
    screen_count: int = 0                           # 实际读了几屏
    raw_lines: list = field(default_factory=list)   # 原始层证据：每屏每行（含重复与坐标）
    section_seen: list = field(default_factory=list)  # 读到的章节（work/edu/…）
    conflicts: list = field(default_factory=list)     # 跨屏冲突（时间段/字段矛盾）
    shots: list = field(default_factory=list)         # [(屏号, numpy 裁切图)]，仅内存
    review_reason: str = ""
    reader: str = "ocr"                             # ocr / vlm
    basic: dict = field(default_factory=dict)       # 年龄/学历性质/毕业年份/求职状态
    missing: list = field(default_factory=list)     # 采集缺失字段（补采后仍缺的）
    name_candidates: list = field(default_factory=list)   # 姓名候选（含来源）
    name_disputed: bool = False                     # 姓名存在分歧
    usage: dict = field(default_factory=dict)       # 视觉模型 token 用量

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "lines": self.lines,
            "text": self.text,
            "sections": self.sections,
            "steps": self.steps,
            "reached_education": self.reached_education,
            "stopped_reason": self.stopped_reason,
            "work": self.work,
            "education": self.education,
            "sidebar": self.sidebar,
            "complete": self.complete,
            "at_top": self.at_top,
            "screen_count": self.screen_count,
            "section_seen": self.section_seen,
            "conflicts": self.conflicts,
            "basic": self.basic,
            "missing": self.missing,
            "name_candidates": self.name_candidates,
            "name_disputed": self.name_disputed,
            "incomplete": self.incomplete,
            "review_reason": self.review_reason,
        }


def panel_band(img: np.ndarray, wide: vision.Rect) -> vision.Rect:
    """弹窗正文的识别区域：以侧栏大按钮为锚点，再按白色像素收窄上下边界。"""
    h, w = img.shape[:2]
    k = vision.scale_of(wide.w, vision.REF_WIDE_W)      # 窗口缩放系数
    x0 = max(0, wide.x - int(round(PANEL_LEFT_OFFSET * k)))
    x1 = min(w, max(x0 + 200, wide.x - int(round(PANEL_RIGHT_OFFSET * k))))
    y0 = int(h * PANEL_TOP_RATIO)
    y1 = int(h * PANEL_BOTTOM_RATIO)

    # 在正文列中间取一窄条，按"接近白色"找真正的内容上下沿
    strip = img[y0:y1, (x0 + x1) // 2 - 40 : (x0 + x1) // 2 + 40].mean(axis=2)
    white_rows = (strip > 246).mean(axis=1) > 0.8
    idx = np.flatnonzero(white_rows)
    if idx.size > (y1 - y0) * 0.3:
        y0 = max(0, y0 + int(idx[0]) - 36)      # 往上留一点，别把第一条经历的表头裁掉
        y1 = y0 + int(idx[-1]) - int(idx[0]) + 1
    return vision.Rect(x0, y0, x1 - x0, max(120, y1 - y0))


def _is_near_dup(text: str, recent: list[str], threshold: float = 0.86) -> bool:
    """和最近几行"几乎一样"的行，视为相邻帧重复采集（OCR 会有个别字差异）。"""
    import difflib

    t = text.strip()
    if not t:
        return True
    for old in recent:
        if abs(len(old) - len(t)) > 4:
            continue
        if difflib.SequenceMatcher(None, old, t).ratio() >= threshold:
            return True
    return False


def _has_education_content(lines: list[dict], min_after: int = 1) -> bool:
    """「教育经历」标题出现，且后面至少还有一条内容（学校/时间那一行）。"""
    for i, ln in enumerate(lines):
        if "教育经历" in ln["text"]:
            return len(lines) - i - 1 >= min_after
    return False


def read_resume(
    rect: tuple[int, int, int, int],
    wide: vision.Rect,
    *,
    max_steps: int = 12,
    scroll_clicks: int = -3,
    settle: float = 0.9,
    log: Callable[[str], None] = print,
) -> ReadResult:
    """滚动并识别整份简历。

    调用前弹窗必须已经打开（sidebar 的「打招呼」大按钮可见）。
    """
    ox, oy = rect[0], rect[1]
    engine = vision.load_ocr()
    if engine is None:
        raise RuntimeError("OCR 未安装，无法逐屏读取")

    import numpy as _np

    import stream_parse

    res = ReadResult()
    parser = stream_parse.StreamParser()
    seen: set[str] = set()
    recent: list[str] = []          # 最近若干行，用于相邻屏重叠去重
    band = None
    prev_frame: bytes | None = None

    # 先把弹窗正文拉回顶部：否则第一条经历的表头可能正好被裁在可视区上方
    try:
        first_img = winenv.grab_window(rect)
        probe = panel_band(first_img, wide)
        tx = ox + probe.x + probe.w // 2
        ty = oy + probe.y + probe.h // 2
        for attempt in range(2):
            before = _np.asarray(first_img, dtype=_np.int16)
            winenv.scroll(tx, ty, 5)
            time.sleep(settle)
            after_img = winenv.grab_window(rect)
            after = _np.asarray(after_img, dtype=_np.int16)
            changed = float(_np.abs(after - before).mean())
            first_img = after_img
            if changed < 0.5:                     # 画面基本没变 -> 认为已经在顶部
                res.at_top = True
                log(f"  已确认弹窗在顶部（第 {attempt + 1} 次上滚，画面变化 {changed:.2f}）")
                break
        else:
            res.at_top = True                     # 滚了两次仍在变，按已到顶部继续
            log("  上滚两次画面仍在变化，按已在顶部继续读取")
    except Exception as exc:
        log(f"  [warn] 回滚到顶部失败（继续读）：{exc}")

    for step in range(max_steps):
        img = winenv.grab_window(rect)
        if band is None:
            band = panel_band(img, wide)
            log(f"  正文识别区: ({band.x},{band.y}) {band.w}x{band.h}")

        items = vision.ocr_crop(engine, img, band)
        if step == 0:
            res.name = guess_name(items)
        new_lines = 0
        for it in items:
            key = it["text"].strip()
            if not key:
                continue
            # 原始证据：每一屏的每一行都留（含重复与坐标），供事后复核
            res.raw_lines.append({"t": key, "s": round(float(it.get("score") or 0), 3),
                                  "b": [int(v) for v in it.get("box", [])], "step": step})
            if key in seen:
                continue
            if _is_near_dup(key, recent):
                continue
            seen.add(key)
            recent.append(key)
            if len(recent) > 80:
                recent.pop(0)
            res.lines.append({**it, "step": step})
            new_lines += 1
        log(f"  第 {step + 1} 屏：{len(items)} 行，新增 {new_lines} 行")

        res.screen_count = step + 1
        if len(res.shots) < 14:                 # 留证据用，内存里最多 14 屏
            res.shots.append((step, img[band.y: band.y + band.h, band.x: band.x + band.w].copy()))
        res.text = "\n".join(ln["text"] for ln in res.lines)

        # 跨屏状态机解析：章节/当前经历/续行都由它维护
        try:
            parser.feed(items, screen=step)
        except Exception as exc:
            log(f"  [warn] 跨屏解析失败：{exc}")

        frame_bytes = img.tobytes()
        moved = prev_frame is not None and frame_bytes != prev_frame
        prev_frame = frame_bytes

        section_done = "edu" in parser.section_seen
        if section_done and parser.education:
            res.reached_education = True
            res.stopped_reason = "读取完成（已读到教育经历）"
            res.complete = True
            break
        if step > 0 and new_lines == 0 and not moved:
            res.stopped_reason = ("页面未移动（判断为到底，未读到教育经历）"
                                  if not section_done else "页面未移动（已到底）")
            break
        if step == max_steps - 1:
            res.stopped_reason = f"达到滚动上限 {max_steps} 屏（未读到教育经历）"
            break

        tx = ox + band.x + band.w // 2
        ty = oy + band.y + int(band.h * 0.75)
        winenv.scroll(tx, ty, scroll_clicks)
        time.sleep(settle)
        res.steps = step + 1

    parser.close()
    res.conflicts = list(parser.conflicts)
    res.work = parser.work
    res.education = parser.education
    res.section_seen = list(parser.section_seen)

    res.steps = max(res.steps, 1)
    res.sections = slice_sections(res.text)
    # 用整段文本再算一次姓名（能看上下文，比单看坐标稳），算出来就覆盖
    better = guess_name_from_text(res.text)
    if better:
        res.name = better

    # 侧栏「经历概览」交叉校验：补公司名/职位，补主区域漏掉的条目
    try:
        sidebar = read_sidebar(engine, winenv.grab_window(rect), wide, log=log)
    except Exception as exc:
        sidebar = []
        log(f"  [warn] 侧栏读取失败：{exc}")
    res.sidebar = sidebar
    merged = {"added_work": 0, "added_edu": 0, "filled": 0, "truncated": 0, "notes": []}
    if sidebar:
        merged = merge_sidebar(res.work, res.education, sidebar)
        log(f"  侧栏合并：补 {merged['filled']} 项，新增 {merged['added_work']} 段，"
            f"截断 {merged['truncated']} 条")

    # 完整性判定：`已读到教育经历` 只是页面进度信号，没读到就说明这份简历没读全
    if not res.reached_education:
        res.incomplete = True
        if "教育经历" not in (res.sections or {}):
            res.review_reason = f"教育经历未读到（{res.stopped_reason or '未知原因'}）"
        else:
            res.review_reason = "教育经历分节为空"
    if not res.work:
        res.review_reason = (res.review_reason + "；" if res.review_reason else "") + "工作经历 0 条"
    else:
        missing = sum(1 for w in res.work
                      if (w.get("company") or "").strip() and not (w.get("title") or "").strip())
        missing_company = sum(1 for w in res.work if not (w.get("company") or "").strip())
        extra = []
        if missing:
            extra.append(f"{missing} 段经历缺职位")
        if missing_company:
            extra.append(f"{missing_company} 段经历缺公司名")
        if merged["added_work"]:
            extra.append(f"{merged['added_work']} 段由侧栏补入")
        if merged["truncated"]:
            extra.append(f"{merged['truncated']} 段侧栏公司名截断")
        if res.conflicts:
            extra.append(f"{len(res.conflicts)} 处跨屏冲突")
        if merged.get("conflicts"):
            extra.append(f"{len(merged['conflicts'])} 处侧栏与正文不一致")
        if extra:
            res.review_reason = "；".join(
                ([res.review_reason] if res.review_reason else []) + extra)
    if res.review_reason:
        res.incomplete = True
    return res


SIDEBAR_X_RATIO = 0.625      # 侧栏「经历概览」大致从窗口宽度的这个比例开始
SIDEBAR_MIN_W = 300


def sidebar_band(img: np.ndarray, wide: vision.Rect) -> vision.Rect:
    """右侧「经历概览」区域：主面板右边到窗口右缘。"""
    h, w = img.shape[:2]
    x0 = min(w - SIDEBAR_MIN_W, max(wide.x - PANEL_RIGHT_OFFSET + 20,
                                    int(w * SIDEBAR_X_RATIO)))
    return vision.Rect(x0, 0, w - x0, h)


def _rows_by_y(items: list[dict], tol: int = 8) -> list[list[dict]]:
    """把 OCR 结果按纵坐标聚成行。"""
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda i: (i["box"][1], i["box"][0])):
        placed = False
        for row in rows:
            if abs(row[0]["box"][1] - it["box"][1]) <= tol:
                row.append(it)
                placed = True
                break
        if not placed:
            rows.append([it])
    for row in rows:
        row.sort(key=lambda i: i["box"][0])
    return rows


def parse_sidebar_items(items: list[dict]) -> list[dict]:
    """解析侧栏「经历概览」：公司 + 时间段一行，职位 + 时长下一行。

    返回 [{company, title, period, truncated}]，按出现顺序。
    """
    import re as _re

    rows = _rows_by_y(items)
    start = None
    for i, row in enumerate(rows):
        if any("经历概览" in it["text"] for it in row):
            start = i + 1
            break
    if start is None:
        return []

    out: list[dict] = []
    pending: dict | None = None
    for row in rows[start:]:
        texts = [it["text"] for it in row]
        joined = " ".join(texts)
        period = next((t for t in texts if _re.search(r"\d{4}", t)), "")
        left = next((t for t in texts if t != period and len(t.strip()) >= 2), "")
        if not left:
            continue
        left_clean = left.strip()
        if period and _re.search(r"\d{4}", period):
            if pending:
                out.append(pending)
            pending = {"company": left_clean, "title": "", "period": period.strip(),
                       "truncated": ".." in left_clean or "…" in left_clean}
        elif pending and not pending["title"]:
            pending["title"] = left_clean
    if pending:
        out.append(pending)
    return out


def read_sidebar(engine, img: np.ndarray, wide: vision.Rect, log=print) -> list[dict]:
    """读右侧「经历概览」（先定位标题，再只 OCR 那一块）。"""
    band = sidebar_band(img, wide)
    probe = vision.Rect(band.x, band.y, min(160, band.w), band.h)
    head_items = vision.ocr_crop(engine, img, probe)
    y_top = 0
    for it in head_items:
        if "经历概览" in it["text"]:
            y_top = max(0, it["box"][1] - 12)
            break
    region = vision.Rect(band.x, y_top, band.w, min(band.h - y_top, 760))
    items = vision.ocr_crop(engine, img, region)
    entries = parse_sidebar_items(items)
    if entries:
        log(f"  侧栏经历概览：{len(entries)} 段")
    return entries


EDU_HINT = ("学院", "大学", "学校", "中学", "职校", "技校")
TRUNC_CHARS = ("..", "…", "．.", "．")


def _norm_period(period: str) -> str:
    import re as _re
    return _re.sub(r"[\s　]", "", period or "")


def _looks_edu(entry: dict) -> bool:
    """只按时间形态判断：教育经历通常只有年份（2021-2024），工作经历带月份。

    不再因为机构名里有「大学/学院」就归为教育 —— 很多公司名也带这类词。
    """
    period = _norm_period(entry.get("period", ""))
    if not period:
        return False
    return "." not in period and period.count("-") == 1


def _is_truncated(name: str) -> bool:
    """侧栏公司名被省略号裁掉了（"泉州市展卷文化传播有..." / "晋江安雅卫生用品有限."）。"""
    t = (name or "").strip()
    if not t:
        return False
    return t.endswith((".", "．", "…", "。。")) or ".." in t or "…" in t


def _similar_name(a: str, b: str, threshold: float = 0.6) -> bool:
    """侧栏公司名常被截断，用前缀/相似度判断是不是同一家。"""
    import difflib

    a, b = (a or "").strip(" ．.…"), (b or "").strip(" ．.…")
    if not a or not b:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def merge_sidebar(work: list[dict], education: list[dict], sidebar: list[dict]) -> dict:
    """把侧栏「经历概览」并进主区域结果：补公司名/职位，补漏掉的条目。

    侧栏公司名常被截断（带 .. 或 ．），截断的不写进结果，只用来校验数量。
    返回 {"added_work": n, "added_edu": n, "filled": n, "truncated": n, "notes": [...]}
    """
    filled = added_work = added_edu = truncated = 0
    notes: list[str] = []
    conflict_sink: list[str] = []

    for ent in sidebar:
        name = (ent.get("company") or "").strip()
        period = _norm_period(ent.get("period", ""))
        title = (ent.get("title") or "").strip()
        is_edu = _looks_edu(ent)
        target = education if is_edu else work

        # 不能用"日期相同"单独认定是同一条：必须公司名也相近（或一侧为空）
        match = None
        for x in target:
            if _norm_period(x.get("period", "")) != period:
                continue
            other = (x.get("company") or x.get("school") or "").strip()
            if not other or not name or _similar_name(other, name):
                match = x
                break
            if other and name and period == _norm_period(x.get("period", "")):
                notes_conflict = f"侧栏与正文同期不同单位：{name} / {other}"
                if notes_conflict not in conflict_sink:
                    conflict_sink.append(notes_conflict)
        if match is not None:
            if not (match.get("company") or match.get("school")):
                if ent.get("truncated") or _is_truncated(name):
                    truncated += 1
                    notes.append(f"{period} 侧栏公司名被截断（{name}）")
                else:
                    match["company" if not is_edu else "school"] = name
                    filled += 1
            if not is_edu and not match.get("title") and title:
                match["title"] = title
                filled += 1
            continue

        # 主区域没有同名条目，但同时间段已有别的单位 -> 只记冲突，不新增摘要条目
        same_period = [x for x in target if _norm_period(x.get("period", "")) == period]
        if same_period:
            other = (same_period[0].get("company") or same_period[0].get("school") or "").strip()
            conflict = f"侧栏与正文同期不同单位：{name} / {other}"
            if conflict not in conflict_sink:
                conflict_sink.append(conflict)
            truncated += 0
            continue

        # 主区域确实没有这一段：侧栏补进来（标记为摘要）
        if ent.get("truncated") or _is_truncated(name):
            truncated += 1
            notes.append(f"{period} 主区域缺这段，侧栏公司名被截断（{name}）")
            continue
        if is_edu:
            education.append({"school": name, "major": "", "degree": "",
                              "period": ent.get("period", ""), "source": "sidebar"})
            added_edu += 1
        else:
            work.append({"company": name, "title": title, "employment": "",
                         "period": ent.get("period", ""), "descriptions": [], "tags": [],
                         "source": "sidebar", "summary_only": True,
                         "notes": ["侧栏摘要：只有单位/职位/时间，正文未读取"]})
            added_work += 1
        notes.append(f"{period} 由侧栏补入（摘要，正文未读取）")

    return {"added_work": added_work, "added_edu": added_edu, "filled": filled,
            "truncated": truncated, "notes": notes, "conflicts": conflict_sink}


def card_name(img, card, log=print) -> str:
    """从列表卡片上读姓名（点开弹窗之前）。

    弹窗可能一打开就已经滚动过、看不到姓名；卡片左上角的名字是稳定可见的，
    用它作为姓名的主要来源，弹窗里的姓名只作交叉校验。
    """
    try:
        engine = vision.load_ocr()
        if engine is None:
            return ""
        region = vision.Rect(card.x + 70, card.y + 8, min(320, card.w - 80), 52)
        items = vision.ocr_crop(engine, img, region)
        cand = guess_name(items) or guess_name_from_text("\n".join(i["text"] for i in items))
        try:
            import vlm as _v
            if not _v.is_plausible_name(cand):
                # 退一步：取区域里第一段像姓名的文本
                for it in items:
                    cleaned = _v.clean_name(it["text"])
                    if _v.is_plausible_name(cleaned):
                        return cleaned
                return ""
        except Exception:
            pass
        return cand
    except Exception as exc:
        log(f"  [warn] 卡片姓名读取失败：{exc}")
        return ""


def read_resume_vlm(rect, wide, log=print, max_steps: int = 10,
                    settle: float = 0.6, scroll_clicks: int = -4,
                    cfg: dict | None = None) -> ReadResult:
    """视觉模型逐屏读取。

    速度相关：每屏滚 -4 格（约 480px，面板高约 1260px，重叠 60% 以上，避免公司名行被切在屏间），
    页面稳定等待 0.6 秒。相比 OCR 版（-3 / 0.9s）每屏省约 0.5 秒，
    但因为一屏能多读 60% 内容，整体屏数少 30~50%。
    """
    """用多模态大模型逐屏读简历（替代 OCR）。

    流程与 OCR 版一致：回顶 -> 逐屏截图 -> 送模型 -> 跨屏合并 -> 读到教育就停。
    区别是没有 OCR 文本，结构化结果直接来自模型对版式的理解。
    """
    import numpy as _np

    import vlm

    res = ReadResult()
    cfg = cfg or vlm.load_config()
    if not (cfg.get("api_key") or vlm.api_key(cfg)):
        raise RuntimeError("没有配置视觉模型 API key（环境变量 VLM_API_KEY）")

    ox, oy = rect[0], rect[1]
    band = panel_band(winenv.grab_window(rect), wide)
    log(f"  正文识别区: ({band.x},{band.y}) {band.w}x{band.h}")

    # 回顶（与 OCR 版同样的验证式回滚）
    try:
        img = winenv.grab_window(rect)
        tx, ty = ox + band.x + band.w // 2, oy + band.y + band.h // 2
        before = _np.asarray(img, dtype=_np.int16)
        winenv.scroll(tx, ty, 5)
        time.sleep(settle)
        after = _np.asarray(winenv.grab_window(rect), dtype=_np.int16)
        res.at_top = float(_np.abs(after - before).mean()) < 0.5
        log("  已把弹窗滚到顶部再开始读")
    except Exception as exc:
        log(f"  [warn] 回滚到顶部失败（继续读）：{exc}")

    work: list[dict] = []
    education: list[dict] = []
    projects: list[dict] = []
    campus: list[dict] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "screens": 0}
    unattributed: list[list[str]] = []          # 找不到归属的职责片段
    pending_fragments: list[dict] = []          # 暂存的跨屏片段（等有经历再挂）
    edu_screens: list[int] = []                 # 出现过教育经历的屏号
    edu_pending = 0                              # 教育字段不全时的补采计数
    prev_frame = None

    for step in range(max_steps):
        img = winenv.grab_window(rect)
        crop = img[band.y: band.y + band.h, band.x: band.x + band.w]
        res.shots.append((step, crop.copy()))

        try:
            raw = vlm.read_screen(crop, cfg, log=log)
        except Exception as exc:
            log(f"  [warn] 第 {step + 1} 屏读取失败：{exc}")
            res.stopped_reason = f"视觉模型调用失败（第 {step + 1} 屏）"
            break

        parts = vlm.normalize(raw["sections"])
        # 跨屏续行片段：归属到上一条经历（滚动后顶部露出的通常是上一段的尾巴）
        # 若当前还没有任何经历（第一屏常常是空的），先暂存，等后面的屏读到经历再挂
        for frag in list(pending_fragments) + list(parts.get("fragments") or []):
            if frag in pending_fragments:
                pending_fragments.remove(frag)
            if not work:
                pending_fragments.append(frag)
                continue
            target = work[-1]
            added = False
            for text in frag.get("descriptions") or []:
                if text and text not in target.setdefault("descriptions", []):
                    target["descriptions"].append(text)
                    added = True
            if added:
                notes = target.setdefault("notes", [])
                if "含跨屏续行" not in notes:
                    notes.append("含跨屏续行")
        if step == 0:
            try:
                res.basic = vlm.normalize_basic(raw)
            except Exception as exc:
                log(f"  [warn] 基本信息解析失败：{exc}")
        elif res.basic is not None:
            # 学习形式/毕业年份经常写在后几屏的教育区，缺了就补上
            try:
                later = vlm.normalize_basic(raw)
                for key in ("education_mode", "graduation_year", "age", "status"):
                    if not res.basic.get(key) and later.get(key):
                        res.basic[key] = later[key]
            except Exception:
                pass
            panel_name = _name_from_vlm(raw)
            # 姓名单独做"紧贴裁剪 + 加权投票"，比整屏问准得多（整屏会把「王伟俐」读成「王伟刚」）
            zoom_name, votes = vlm.read_name_consensus(img, band, cfg, log=log)
            # 读不到像样的姓名 -> 弹窗可能还没回到顶部，强行再回顶一次重读
            if not zoom_name:
                log("  姓名区没拍到（可能没回顶），再滚一次顶部重读")
                try:
                    tx = ox + band.x + band.w // 2
                    ty = oy + band.y + band.h // 2
                    winenv.scroll(tx, ty, 8)
                    time.sleep(0.8)
                    img = winenv.grab_window(rect)
                    band = panel_band(img, wide)
                    zoom_name, votes = vlm.read_name_consensus(img, band, cfg, log=log)
                    if zoom_name:
                        log(f"  重读成功: {zoom_name}")
                except Exception as exc:
                    log(f"  [warn] 重新回顶失败：{exc}")
            res.name = zoom_name or panel_name
            candidates = [v for v in dict.fromkeys(([panel_name] if panel_name else []) + list(votes)) if v]
            res.name_candidates = candidates
            if len(set(candidates)) > 1:
                res.name_disputed = True
                res.conflicts.append(
                    f"姓名存在分歧：候选 {'/'.join(candidates)}（暂取 {res.name}）")
                log(f"  ⚠ 姓名分歧：{candidates}，暂取 {res.name}")
            if not res.name:
                log(f"  [warn] 姓名未识别，票={votes}")
        if parts["education"] and step not in edu_screens:
            edu_screens.append(step)
        before = len(work)
        vlm.merge_screens(work, parts["work"])
        for entry in work[before:]:          # 这一屏新增的条目：记住来自第几屏
            entry["_screen"] = step
        vlm.merge_screens(projects, parts["projects"])
        vlm.merge_screens(campus, parts["campus"])
        for e in parts["education"]:
            same = next((old for old in education
                         if _norm_period(e.get("period", "")) == _norm_period(old.get("period", ""))
                         and (not e.get("school") or not old.get("school")
                              or e.get("school") == old.get("school"))), None)
            if same is None:
                education.append(e)
            else:
                # 同一段教育在多屏重复出现：补缺失字段（学校/专业/学历），而不是整体跳过
                for field in ("school", "major", "degree", "period"):
                    if not (same.get(field) or "").strip() and (e.get(field) or "").strip():
                        same[field] = e[field]
        res.screen_count = step + 1
        u = raw.get("usage") or {}
        usage_total["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage_total["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage_total["screens"] += 1
        log(f"  第 {step + 1} 屏：工作 {len(parts['work'])} 条 / 教育 {len(parts['education'])} 条")

        if step == 0 and not res.name:
            res.name = _name_by_ocr(img, band, log=log)

        frame = img.tobytes()
        moved = prev_frame is not None and frame != prev_frame
        prev_frame = frame

        if education:
            first = education[0]
            edu_ok = bool((first.get("school") or "").strip() and (first.get("degree") or "").strip())
            if edu_ok:
                res.reached_education = True
                res.complete = True
                res.stopped_reason = "读取完成（第一段教育字段完整）"
                break
            # 有教育条目但字段不全 -> 再往下滚一到两屏补采，别急着结束
            edu_pending += 1
            log(f"  第一段教育字段不全（{first.get('school') or '缺学校'}/"
                f"{first.get('degree') or '缺学历'}），继续向下补采 {edu_pending}/2")
            if edu_pending >= 2:
                res.reached_education = True
                res.stopped_reason = "第一段教育字段不全（已补采 2 屏仍未补齐）"
                break
        if step > 0 and not moved:
            res.stopped_reason = "页面未移动（判断为到底，未读到教育经历）"
            break
        if step == max_steps - 1:
            res.stopped_reason = f"达到滚动上限 {max_steps} 屏（未读到教育经历）"
            break

        tx = ox + band.x + band.w // 2
        ty = oy + band.y + int(band.h * 0.75)
        winenv.scroll(tx, ty, scroll_clicks)
        time.sleep(settle)
        res.steps = step + 1

    # 循环结束时还有暂存片段：有经历就挂到最后一条，否则算未归属
    if pending_fragments:
        if work:
            for frag in pending_fragments:
                for text in frag.get("descriptions") or []:
                    if text and text not in work[-1].setdefault("descriptions", []):
                        work[-1]["descriptions"].append(text)
            log(f"  尾部片段 {len(pending_fragments)} 段已挂到最后一条经历")
        else:
            for frag in pending_fragments:
                unattributed.append(frag.get("descriptions") or [])
        pending_fragments.clear()

    # ---------- 定向补采 0：学习形式/毕业年份缺失 -> 回到教育屏放大重读 ----------
    try:
        need_mode = not (res.basic.get("education_mode") or "").strip()
        need_year = not res.basic.get("graduation_year")
        if education and (need_mode or need_year):
            zoom_cfg2 = dict(cfg)
            zoom_cfg2["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
            shots_by_step2 = {step: crop for step, crop in res.shots}
            targets = [st for st in (edu_screens or []) if st in shots_by_step2]
            if not targets:
                targets = list(shots_by_step2)[-1:]
            for st in targets[:2]:
                crop = shots_by_step2.get(st)
                if crop is None:
                    continue
                log(f"  教育信息不全（学习形式={res.basic.get('education_mode') or '空'}/"
                    f"毕业={res.basic.get('graduation_year') or '空'}），放大重读第 {st + 1} 屏")
                try:
                    raw4 = vlm.read_screen(crop, zoom_cfg2, log=log)
                except Exception as exc:
                    log(f"    [warn] 重读失败：{exc}")
                    continue
                u4 = raw4.get("usage") or {}
                usage_total["prompt_tokens"] += int(u4.get("prompt_tokens") or 0)
                usage_total["completion_tokens"] += int(u4.get("completion_tokens") or 0)
                usage_total["screens"] += 1
                b4 = vlm.normalize_basic(raw4)
                if not (res.basic.get("education_mode") or "").strip() and b4.get("education_mode"):
                    res.basic["education_mode"] = b4["education_mode"]
                    log(f"    补回学习形式：{b4['education_mode']}")
                if not res.basic.get("graduation_year") and b4.get("graduation_year"):
                    res.basic["graduation_year"] = b4["graduation_year"]
                    log(f"    补回毕业年份：{b4['graduation_year']}")
                # OCR 兜底：直接找"全日制/非全日制/统招"字样
                if not (res.basic.get("education_mode") or "").strip():
                    mode_ocr = vlm.scan_education_mode(crop)
                    if mode_ocr:
                        res.basic["education_mode"] = mode_ocr
                        res.basic["education_mode_source"] = "ocr"
                        log(f"    OCR 判定学习形式：{mode_ocr}")
    except Exception as exc:
        log(f"  [warn] 教育信息补采失败：{exc}")

    # 毕业年份兜底：用教育起止的结束年
    try:
        if not res.basic.get("graduation_year") and education:
            import re as _re
            years = [int(y) for y in _re.findall(r"(19|20)\d{2}", education[0].get("period") or "")]
            if years:
                res.basic["graduation_year"] = max(years)
                log(f"  毕业年份按教育起止推定：{res.basic['graduation_year']}")
    except Exception:
        pass

    # ---------- 定向补采 A：缺公司名/职位 -> 回到那一屏放大重读 ----------
    try:
        import completeness as _cmp2
        need_company = [w for w in work
                        if not (w.get("company") or "").strip()
                        or not (w.get("title") or "").strip()]
        if need_company:
            zoom_cfg = dict(cfg)
            zoom_cfg["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
            shots_by_step = {step: crop for step, crop in res.shots}
            fixed = 0
            for entry in need_company:
                step = entry.get("_screen")
                crop = shots_by_step.get(step)
                if crop is None:
                    continue
                log(f"  第 {step + 1} 屏有经历缺公司名/职位，放大重读该屏")
                try:
                    raw3 = vlm.read_screen(crop, zoom_cfg, log=log)
                except Exception as exc:
                    log(f"    [warn] 该屏重读失败：{exc}")
                    continue
                parts3 = vlm.normalize(raw3["sections"])
                u3 = raw3.get("usage") or {}
                usage_total["prompt_tokens"] += int(u3.get("prompt_tokens") or 0)
                usage_total["completion_tokens"] += int(u3.get("completion_tokens") or 0)
                usage_total["screens"] += 1
                match = _match_entry(entry, parts3["work"])
                if match and (match.get("company") or "").strip():
                    entry["company"] = match["company"]
                    if not (entry.get("title") or "").strip() and match.get("title"):
                        entry["title"] = match["title"]
                    if not (entry.get("period") or "").strip() and match.get("period"):
                        entry["period"] = match["period"]
                    fixed += 1
                    log(f"    补回：{entry['company']} / {entry.get('title') or ''}")
            if fixed:
                log(f"  按屏补回 {fixed} 条经历的公司名/职位")
    except Exception as exc:
        log(f"  [warn] 按屏补采失败：{exc}")

    # ---------- 定向补采：缺职责就放大当前屏重读 ----------
    try:
        import completeness as _cmp

        def _snapshot():
            return {"name": res.name, "basic": res.basic, "education": education,
                    "work": work, "collection": {"complete": res.complete}}

        ok, missing, nice = _cmp.check(_snapshot())
        lacks_duty = any("职责" in m for m in missing)
        if lacks_duty and max_steps > 0:
            log("  有经历缺职责，放大当前屏重读一次")
            try:
                cfg_zoom = dict(cfg)
                cfg_zoom["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
                img2 = winenv.grab_window(rect)
                band2 = panel_band(img2, wide)
                raw2 = vlm.read_screen(img2[band2.y:band2.y + band2.h, band2.x:band2.x + band2.w],
                                      cfg_zoom, log=log)
                parts2 = vlm.normalize(raw2["sections"])
                vlm.merge_screens(work, parts2["work"])
                u2 = raw2.get("usage") or {}
                usage_total["prompt_tokens"] += int(u2.get("prompt_tokens") or 0)
                usage_total["completion_tokens"] += int(u2.get("completion_tokens") or 0)
                usage_total["screens"] += 1
                log(f"  放大重读后：工作 {len(work)} 条")
            except Exception as exc:
                log(f"  [warn] 放大重读失败：{exc}")

        ok, missing, nice = _cmp.check(_snapshot())
        res.missing = missing
        if missing:
            log(f"  采集仍有缺失：{_cmp.summarize(missing)}")
        elif nice:
            log(f"  采集完整（建议补充：{'、'.join(nice[:3])}）")
    except Exception as exc:
        log(f"  [warn] 完整性检查失败：{exc}")

    res.work = work
    res.education = education
    if projects:
        res.conflicts.extend([])
    res.usage = usage_total
    res.reader = "vlm"
    res.section_seen = (["work"] if work else []) + (["edu"] if education else [])

    # 复核判定（与 OCR 版一致）
    if not res.name:
        res.review_reason = "姓名未识别"
    if res.name_disputed:
        res.review_reason = ((res.review_reason + "；") if res.review_reason else "") + \
            f"姓名有分歧（候选 {'/'.join(res.name_candidates)}）"
    if res.conflicts:
        res.review_reason = ((res.review_reason + "；") if res.review_reason else "") + \
            f"{len(res.conflicts)} 处冲突"
    if unattributed:
        res.review_reason = ((res.review_reason + "；") if res.review_reason else "") + \
            f"{len(unattributed)} 段职责无法归属"
    if not education:
        res.incomplete = True
        res.review_reason = ((res.review_reason + "；") if res.review_reason else "") + \
            f"教育经历未读到（{res.stopped_reason or '未知原因'}）"
    if not work:
        res.review_reason = (res.review_reason + "；" if res.review_reason else "") + "工作经历 0 条"
    else:
        miss_title = sum(1 for w in work if (w.get("company") or "").strip() and not (w.get("title") or "").strip())
        miss_company = sum(1 for w in work if not (w.get("company") or "").strip())
        extra = []
        if miss_title:
            extra.append(f"{miss_title} 段经历缺职位")
        if miss_company:
            extra.append(f"{miss_company} 段经历缺公司名")
        if extra:
            res.review_reason = "；".join(([res.review_reason] if res.review_reason else []) + extra)
    if res.review_reason:
        res.incomplete = True
    return res


def _norm_text(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isalnum())


def _match_entry(target: dict, candidates: list[dict]) -> dict | None:
    """在同一屏的重读结果里找回同一条经历（先按职责重叠，再按时间）。"""
    t_duties = [_norm_text(x) for x in (target.get("descriptions") or []) if _norm_text(x)]
    t_period = (target.get("period") or "").replace(" ", "")

    best, best_score = None, 0
    for cand in candidates or []:
        c_duties = [_norm_text(x) for x in (cand.get("descriptions") or []) if _norm_text(x)]
        score = 0
        for td in t_duties:
            for cd in c_duties:
                if td and cd and (td in cd or cd in td):
                    score += 3
                    break
        c_period = (cand.get("period") or "").replace(" ", "")
        if t_period and c_period and t_period == c_period:
            score += 5
        title = _norm_text(target.get("title"))
        if title and title in _norm_text(cand.get("title")):
            score += 2
        if score > best_score:
            best, best_score = cand, score

    # 只有在证据足够时才认（至少一条职责重合 或 时间相同）
    if best_score >= 3:
        return best
    # 一屏里只有一条经历时，直接认
    if len(candidates or []) == 1:
        return candidates[0]
    return None

def _name_by_ocr(img, band, log=print) -> str:
    """姓名兜底：只在顶部一小块跑 OCR（约 0.2 秒），模型没读到时用。"""
    try:
        engine = vision.load_ocr()
        if engine is None:
            return ""
        region = vision.Rect(band.x, band.y, min(360, band.w), min(70, band.h))
        items = vision.ocr_crop(engine, img, region)
        cand = guess_name(items) or guess_name_from_text("\n".join(it["text"] for it in items))
        try:
            import vlm as _v
            return cand if _v.is_plausible_name(cand) else ""
        except Exception:
            return cand
    except Exception as exc:
        log(f"  [warn] 姓名 OCR 兜底失败：{exc}")
        return ""


def _name_from_vlm(raw: dict) -> str:
    """视觉模型直接在 JSON 里返回姓名；这里做一次兜底清洗。"""
    name = (raw.get("name") or "").strip()
    for junk in ("刚刚活跃", "今日活跃", "本周活跃", "在线", "月内到岗", "随时到岗"):
        name = name.replace(junk, "")
    return name.strip(" ·|")


def slice_sections(text: str) -> dict[str, str]:
    """按小节标题把全文切段。"""
    starts = SECTION_HEADS
    out: dict[str, str] = {}
    for i, head in enumerate(starts):
        pos = text.find(head)
        if pos < 0:
            continue
        end = len(text)
        for other in SECTION_HEADS:
            if other == head:
                continue
            p2 = text.find(other, pos + len(head))
            if 0 <= p2 < end:
                end = p2
        out[head] = text[pos:end].strip()
    return out
