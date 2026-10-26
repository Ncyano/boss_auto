"""在简历弹窗内部滚动，把整份简历（尤其教育经历）读完。

三个要点
--------
1. **滚在弹窗里**，不是滚整个页面：滚轮指针必须落在弹窗正文区域上。
2. **逐屏读 + 去重拼接**：每滚一次识别一屏，只把没见过的行追加进去。
3. **滚动本身也要过输入闸门**：`winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)` 会先确认光标到位，不到位直接抛
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
import vlm                       # 读屏主通道（OCR / 视觉模型）
import judge                     # 文本结构化

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
    name_issues: list = field(default_factory=list) # 姓名诊断（只记录，不阻断判定）
    work_state: str = "unknown"                     # present / explicit_none / unknown
    work_evidence: str = ""                         # 判 explicit_none 的依据（可追溯）
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
            "name_issues": self.name_issues,
            "work_state": self.work_state,
            "work_evidence": self.work_evidence,
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
            winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
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
        winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
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
                    cfg: dict | None = None, name_hint: str = "") -> ReadResult:
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
        winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
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
    text_all: list[str] = []                    # 所有屏的 OCR 原文（判断区块是否存在）
    edu_pending = 0                              # 教育字段不全时的补采计数
    prev_frame = None

    for step in range(max_steps):
        img = winenv.grab_window(rect)
        crop = img[band.y: band.y + band.h, band.x: band.x + band.w]
        res.shots.append((step, crop.copy()))

        try:
            pipeline = str(cfg.get("pipeline") or "vlm").lower()
            if pipeline == "ocr":
                # 快一倍：专用 OCR 读文字（~7s），再用文本模型结构化（~3s）
                ocr_res = vlm.read_screen_text(crop, cfg, log=log)
                otext = ocr_res.get("text") or ""
                try:
                    import judge as _judge
                    st = _judge.structure_text(otext, _judge.load_config())
                    sdata = st.get("data") or {}
                    try:
                        merged_usage = _merge_usage(ocr_res.get("usage"), st.get("usage"))
                    except Exception:
                        merged_usage = ocr_res.get("usage") or {}
                    raw = {"name": (sdata.get("name") or "").strip(),
                           "basic": sdata.get("basic") or {},
                           "sections": sdata.get("sections") or [],
                           "raw": otext,
                           "usage": merged_usage}
                except Exception as exc:
                    log(f"  [warn] 结构化失败，改用 OCR 原文兜底：{exc}")
                    raw = {"name": "", "basic": {}, "sections": [], "raw": otext,
                           "usage": ocr_res.get("usage") or {}}
            else:
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

            panel_name = _name_from_vlm(raw)
            # 卡片姓名（列表上 OCR 出来的）与面板姓名一致 -> 直接采信，省掉 1~3 次投票调用
            if name_hint and panel_name and name_hint == panel_name:
                log(f"  姓名与列表卡片一致（{panel_name}），跳过投票")
                zoom_name, votes = panel_name, [panel_name]
            else:
                # 姓名单独做"紧贴裁剪 + 加权投票"，比整屏问准得多（整屏会把「王伟俐」读成「王伟刚」）
                zoom_name, votes = vlm.read_name_consensus(img, band, cfg, log=log)
                # 读不到像样的姓名 -> 弹窗可能还没回到顶部，强行再回顶一次重读
                if not zoom_name:
                    log("  姓名区没拍到（可能没回顶），再滚一次顶部重读")
                    try:
                        tx = ox + band.x + band.w // 2
                        ty = oy + band.y + band.h // 2
                        winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
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
                res.name_issues.append(
                    f"姓名有分歧：{'/'.join(candidates)}（暂取 {res.name}）")
                log(f"  ⚠ 姓名分歧：{candidates}，暂取 {res.name}")
            if not res.name:
                log(f"  [warn] 姓名未识别，票={votes}")
        elif res.basic is not None:
            # 学习形式/毕业年份经常写在后几屏的教育区，缺了就补上
            try:
                later = vlm.normalize_basic(raw)
                for key in ("education_mode", "graduation_year", "age", "status"):
                    if not res.basic.get(key) and later.get(key):
                        res.basic[key] = later[key]
            except Exception:
                pass
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
        text_all.append((step, str(raw.get("raw") or "")))
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
            # 一屏都没读到内容（工作/教育都空）-> 可能是渲染没出来，再等再试一次
            if not work and not education and step < 2:
                log("  整屏没读到内容，等 1 秒重试")
                time.sleep(1.0)
                continue
            res.stopped_reason = "页面未移动（判断为到底，未读到教育经历）"
            break
        if step == 0 and not work and not education:
            # 第 1 屏空白：不判到底，先滚一次找内容
            log("  第 1 屏没读到内容，向下滚一屏再试")
        if step == max_steps - 1:
            res.stopped_reason = f"达到滚动上限 {max_steps} 屏（未读到教育经历）"
            break

        tx = ox + band.x + band.w // 2
        ty = oy + band.y + int(band.h * 0.75)
        winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
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

    # ---------- 定向补采 -1：一条工作经历都没读到 -> 放大重读所有屏 ----------
    try:
        if not work and res.shots:
            zoom_cfg0 = dict(cfg)
            zoom_cfg0["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
            for st0, crop0 in res.shots:      # 所有屏都补，不只前三屏
                log(f"  没读到工作经历，放大重读第 {st0 + 1} 屏")
                try:
                    if str(cfg.get("pipeline") or "vlm").lower() == "ocr":
                        otext0 = (vlm.read_screen_text(crop0, zoom_cfg0, log=log) or {}).get("text", "")
                        import judge as _j0
                        sdata0 = (_j0.structure_text(otext0, _j0.load_config()) or {}).get("data") or {}
                        parts0 = {"work": [], "education": sdata0.get("sections") or []}
                        parts0 = vlm.normalize(sdata0.get("sections"))
                    else:
                        raw0 = vlm.read_screen(crop0, zoom_cfg0, log=log)
                        parts0 = vlm.normalize(raw0["sections"])
                    vlm.merge_screens(work, parts0["work"])
                    if parts0["education"] and not education:
                        education.extend(parts0["education"])
                    log(f"    第 {st0 + 1} 屏补回工作 {len(parts0['work'])} 段（累计 {len(work)}）")
                except Exception as exc:
                    log(f"    [warn] 重读失败：{exc}")
    except Exception as exc:
        log(f"  [warn] 工作经历兜底失败：{exc}")

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

    # ---------- 定向补采 0b：学校名二次校验（VLM 易把"长江"读成"长沙"） ----------
    try:
        if education and edu_screens:
            shots_by_step3 = {step: crop for step, crop in res.shots}
            crop3 = shots_by_step3.get(edu_screens[0])
            school_now = (education[0].get("school") or "").strip()
            if crop3 is not None and school_now:
                fixed, note = _verify_school_name(crop3, school_now, cfg, log=log)
                if fixed != school_now:
                    education[0]["school"] = fixed
                    education[0]["school_raw"] = school_now
                    education[0]["school_corrected"] = note
    except Exception as exc:
        log(f"  [warn] 校名二次校验失败：{exc}")

    # 毕业年份兜底：用教育起止的结束年
    try:
        if not res.basic.get("graduation_year") and education:
            import re as _re
            years = [int(y) for y in _re.findall(r"(?<!\d)(\d{4})(?!\d)",
                                                  education[0].get("period") or "")
                     if 1980 <= int(y) <= 2035]
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

        try:
            _n_fixed = _fill_company_from_text(work, [t for _st, t in text_all]
                                               if "text_all" in locals() else [],
                                               log=log)
            # work 是原地修改的，不需要重建
        except Exception as exc:
            log(f"  [warn] 公司名兜底失败：{exc}")

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

        try:
            _n_fixed = _fill_company_from_text(work, [t for _st, t in text_all]
                                               if "text_all" in locals() else [],
                                               log=log)
            # work 是原地修改的，不需要重建
        except Exception as exc:
            log(f"  [warn] 公司名兜底失败：{exc}")

        ok, missing, nice = _cmp.check(_snapshot())
        res.missing = missing

        # 用最终结果重写复核原因：读的过程中报的"缺公司名"可能已经被补采解决，
        # 不重算会让已经完整的简历仍被判采集不完整
        keep = [x for x in (res.review_reason or "").split("；")
                if x and not any(k in x for k in
                                 ("缺公司", "缺职位", "缺起止", "缺职责", "缺学校", "缺学历",
                                  "采集缺失", "无法归属", "工作经历 0 条", "姓名"))]
        if missing:
            keep.append(_cmp.summarize(missing))
        res.review_reason = "；".join(dict.fromkeys(keep))

        if missing:
            log(f"  采集仍有缺失：{_cmp.summarize(missing)}")
        elif nice:
            log(f"  采集完整（建议补充：{'、'.join(nice[:3])}）")
    except Exception as exc:
        log(f"  [warn] 完整性检查失败：{exc}")

    # ---- 工作经历状态：读到 / 明确没有 / 未确认 ----
    try:
        if "text_all" in locals():
            joined = "\n".join(t for _st, t in text_all)
        else:
            joined = str(getattr(res, "text", "") or "")
            text_all = []

        if work:
            res.work_state = "present"
        else:
            # explicit_none 只接受两类证据：
            #   a) 简历本人明写"无工作经历/暂无工作经验"
            #   b) 标题紧邻 —— 还必须回看该屏截图用本地 OCR 复核确认中间确实为空
            empty_marks = ("无工作经历", "暂无工作经历", "没有工作经历", "无工作经验", "暂无工作经验")
            work_evidence = ""
            explicit = False
            for m in empty_marks:
                if m in joined:
                    work_evidence = f"简历写明「{m}」"
                    explicit = True
                    break

            if not explicit:
                heads = ("工作经历", "实习经历", "教育经历", "项目经历", "校园经历",
                         "专业技能", "资格证书", "所获荣誉", "自我评价")
                for step_i, txt in text_all:
                    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
                    cand = None
                    for i, ln in enumerate(lines):
                        if ln.startswith(("工作经历", "实习经历")):
                            nxt = [j for j in range(i + 1, len(lines))
                                   if lines[j].startswith(heads[2:])]
                            if nxt and (nxt[0] - i) == 1:
                                cand = (lines[i][:6], lines[nxt[0]][:6])
                            break
                    if not cand:
                        continue
                    # 回看截图：用本地 OCR 再读一遍。
                    # 注意：两套识别都没读到 ≠ 证明区域为空（它们可能漏同一块），
                    # 所以这里只记录结果、保留截图，**不据此判 explicit_none**，
                    # 一律保持 unknown，交给人工看截图确认。
                    crop_map = {st: c for st, c in res.shots}
                    ok, why = _verify_empty_work_block(crop_map.get(step_i))
                    log(f"  第 {step_i + 1} 屏「工作经历」区块疑似为空，二次识别：{why}"
                        f"（{'两次都没读到' if ok else '有内容或无法判定'}，"
                        f"保持 unknown，截图已留档）")
                    break

            res.work_state = "explicit_none" if explicit else "unknown"
        res.work_evidence = work_evidence if res.work_state == "explicit_none" else ""
        if res.work_state == "explicit_none" and not work:
            log(f"  工作经历：确认无（证据：{work_evidence or '区块为空'}）")
    except Exception as exc:
        res.work_state = "present" if work else "unknown"
        log(f"  [warn] 工作经历状态判定失败：{exc}")

    res.work = work
    res.education = education
    if projects:
        res.conflicts.extend([])
    res.usage = usage_total
    res.reader = "vlm"
    res.section_seen = (["work"] if work else []) + (["edu"] if education else [])

    # 复核判定（与 OCR 版一致）
    if not res.name:
        res.name_issues.append("姓名未识别")
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

def read_resume_fast(rect, wide, log=print, max_screens: int = 10,
                     settle: float = 0.6, scroll_clicks: int = -3,
                     cfg: dict | None = None, name_hint: str = "") -> "ReadResult":
    """快速读法：先把所有屏截下来，再并行 OCR，最后一次性结构化。

    相比逐屏"OCR + 结构化"，2 屏以上能省一半时间：
        ���屏：截图 2s + 并行 OCR 7s + 结构化 4s ≈ 13s（原 20.8s）
        3 屏：截图 3s + 并行 OCR 7s + 结构化 4s ≈ 14s（原 31s）
    """
    from concurrent.futures import ThreadPoolExecutor

    cfg = cfg or vlm.load_config()
    res = ReadResult()
    res.reader = "vlm-fast"

    # ---------- 1. 回到顶部 ----------
    band0 = None
    try:
        img0 = winenv.grab_window(rect)
        band0 = panel_band(img0, wide)
        winenv.scroll_modal(-8, rect[0] + band0.x + band0.w // 2,
                            rect[1] + band0.y + band0.h // 2)
        time.sleep(settle)
    except Exception as exc:
        log(f"  [warn] 回顶失败：{exc}")

    # ---------- 2. 连续截屏（页面不再移动就停） ----------
    crops: list = []
    prev = None
    for _ in range(max_screens):
        img = winenv.grab_window(rect)
        band = panel_band(img, wide)
        crop = img[band.y: band.y + band.h, band.x: band.x + band.w]
        if prev is not None and crop.shape == prev.shape and (crop == prev).all():
            break                       # 这一屏和上一屏完全相同 -> 到底了
        crops.append(crop.copy())
        prev = crop
        try:
            winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
        except Exception:
            break
        time.sleep(settle)

    res.screen_count = len(crops)
    log(f"  快速读法：截到 {len(crops)} 屏")

    # ---------- 3. 并行 OCR ----------
    texts: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(crops)))) as pool:
            futures = [pool.submit(vlm.read_screen_text, c, cfg) for c in crops]
            for fut in futures:
                try:
                    texts.append((fut.result() or {}).get("text", ""))
                except Exception as exc:
                    log(f"    [warn] 某屏 OCR 失败：{exc}")
                    texts.append("")
    except Exception as exc:
        log(f"  [warn] 并行 OCR 失败：{exc}")

    # 相邻屏有重叠，去掉重复行
    merged_lines: list[str] = []
    for text in texts:
        for ln in (text or "").splitlines():
            ln = ln.strip()
            if ln and (not merged_lines or ln != merged_lines[-1]):
                merged_lines.append(ln)
    full_text = "\n".join(merged_lines)
    res.text = full_text

    # ---------- 4. 一次性结构化 ----------
    data = {}
    try:
        st = judge.structure_text(full_text, judge.load_config())
        data = (st or {}).get("data") or {}
    except Exception as exc:
        log(f"  [warn] 结构化失败：{exc}")

    parts = {"work": [], "education": [], "projects": [], "campus": []}
    try:
        parts = vlm.normalize(data.get("sections"))
    except Exception as exc:
        log(f"  [warn] 结构化结果归一失败：{exc}")

    res.work = parts.get("work") or []
    res.education = parts.get("education") or []
    res.basic = dict(data.get("basic") or {})
    res.name = (data.get("name") or "").strip()
    # 列表卡片上的姓名更可信：结构化常常把正文里的其他人名当姓名
    if name_hint and vlm.is_plausible_name(name_hint):
        if res.name and res.name != name_hint:
            log(f"  姓名以列表卡片为准：{name_hint}（结构化读成 {res.name}）")
            res.name_issues.append(f"姓名结构化读作 {res.name}，以卡片 {name_hint} 为准")
        res.name = name_hint
    res.complete = bool(res.work or res.education)

    # ---------- 5. 姓名：结构里没有就用紧贴裁剪补 ----------
    if not res.name and crops:
        try:
            head_band = vision.Rect(0, 0, crops[0].shape[1], min(260, crops[0].shape[0]))
            nm, votes = vlm.read_name_consensus(crops[0], head_band, cfg, log=log)
            res.name = nm
            res.name_candidates = list(votes)
        except Exception as exc:
            log(f"  [warn] 姓名补读失败：{exc}")

    # ---------- 6. 年龄 / 学习形式 / 状态：先按文本兜底 ----------
    basic = res.basic
    try:
        if not basic.get("age") and crops:
            engine = vision.load_ocr()
            if engine is not None:
                head = crops[0][:min(300, crops[0].shape[0]), :]
                items = vision.ocr_crop(engine, head,
                                        vision.Rect(0, 0, head.shape[1], head.shape[0]))
                head_text = " ".join((it.get("text") or "") for it in items)
                got = _basic_from_text(head_text)
                if got.get("age"):
                    basic["age"] = got["age"]
                    log(f"  年龄从头部补回：{got['age']}")
    except Exception:
        pass
    try:
        for k, v in _basic_from_text(full_text).items():
            if not basic.get(k):
                basic[k] = v
    except Exception:
        pass
    if not basic.get("education_mode"):
        for crop in crops:
            try:
                hit = vlm.scan_education_mode(crop)
                if hit:
                    basic["education_mode"] = hit
                    break
            except Exception:
                pass
    if res.education:
        edu0 = res.education[0]
        if not basic.get("graduation_year"):
            years = [int(y) for y in __import__("re").findall(
                r"(?<!\d)(\d{4})(?!\d)", edu0.get("period") or "") if 1980 <= int(y) <= 2035]
            if years:
                basic["graduation_year"] = str(max(years))
    res.basic = basic

    # ---------- 6a. 工作经历兜底：文本里有公司+年份却没结构化出来 ----------
    if not res.work and full_text:
        try:
            guess = _heuristic_work_entries(full_text)
            if guess:
                res.work = guess
                log(f"  工作经历按原文硬捞：{len(guess)} 段"
                    f"（{guess[0]['company']} 等）")
        except Exception as exc:
            log(f"  [warn] 工作经历硬捞失败：{exc}")

    # ---------- 6a2. 没读到教育经历：滚到底再截一屏（长简历前半段看不到） ----------
    if not res.education:
        try:
            log("  没读到教育经历，滚到页面底部再读一屏")
            for _ in range(10):
                band_r = panel_band(winenv.grab_window(rect), wide)
                winenv.scroll_modal(None or -3, rect[0] + band.x + band.w // 2, rect[1] + band.y + band.h // 2)
                time.sleep(0.25)
            img_b = winenv.grab_window(rect)
            band_b = panel_band(img_b, wide)
            crop_b = img_b[band_b.y: band_b.y + band_b.h, band_b.x: band_b.x + band_b.w]
            otext_b = (vlm.read_screen_text(crop_b, cfg) or {}).get("text", "")
            if otext_b:
                texts.append(otext_b)
                st_b = judge.structure_text(otext_b, judge.load_config())
                parts_b = vlm.normalize((st_b or {}).get("data", {}).get("sections"))
                if parts_b.get("education"):
                    res.education = parts_b["education"]
                    log(f"    底部屏补回教育 {len(parts_b['education'])} 条")
                if parts_b.get("work"):
                    vlm.merge_screens(res.work, parts_b["work"])
        except Exception as exc:
            log(f"  [warn] 底部补采失败：{exc}")

    # ---------- 6b. 教育区定向补采（校名双读 + 缺失字段放大重读） ----------
    try:
        edu_crop = None
        for i, text in enumerate(texts):
            if text and ("教育经历" in text):
                edu_crop = crops[i] if i < len(crops) else None
                break

        # 校名双读校验：结构读的校名和 OCR 逐字读的不一致时，放大重读投票
        if edu_crop is not None and res.education:
            now_school = (res.education[0].get("school") or "").strip()
            if now_school:
                fixed, note = _verify_school_name(edu_crop, now_school, cfg, log=log)
                if fixed and fixed != now_school:
                    res.education[0]["school"] = fixed
                    res.education[0]["school_raw"] = now_school
                    res.education[0]["school_corrected"] = note

        # 教育字段缺失（学校/学历/专业） -> 放大重读该屏一次
        need_edu = (not res.education) or any(
            not (res.education[0].get(k) or "").strip() for k in ("school", "degree"))
        if need_edu and edu_crop is not None:
            log("  教育信息不全，放大重读教育屏")
            zoom = dict(cfg)
            zoom["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
            otext = (vlm.read_screen_text(edu_crop, zoom, log=log) or {}).get("text", "")
            if otext:
                st2 = judge.structure_text(otext, judge.load_config())
                parts2 = vlm.normalize((st2 or {}).get("data", {}).get("sections"))
                if parts2.get("education"):
                    vlm.merge_screens(res.education, parts2["education"])
                    log(f"    补回教育 {len(parts2['education'])} 条")
    except Exception as exc:
        log(f"  [warn] 教育区补采失败：{exc}")

    # ---------- 7. 公司名兜底 ----------
    try:
        _fill_company_from_text(res.work, texts, log=log)
    except Exception:
        pass

    # ---------- 8. 完整性与原因 ----------
    try:
        import completeness as _cmp
        ok, missing, nice = _cmp.check({
            "name": res.name, "basic": res.basic, "education": res.education,
            "work": res.work, "collection": {"complete": res.complete}})
        res.missing = missing
        if missing:
            res.review_reason = _cmp.summarize(missing)
            log(f"  采集仍有缺失：{res.review_reason}")
        elif nice:
            log(f"  采集完整（建议补充：{'、'.join(nice[:3])}）")
    except Exception as exc:
        log(f"  [warn] 完整性检查失败：{exc}")

    res.work_state = "present" if res.work else "unknown"
    if not res.work:
        try:
            marks = ("无工作经历", "暂无工作经历", "没有工作经历", "无工作经验", "暂无工作经验")
            hit = next((m for m in marks if m in full_text), "")
            if hit:
                res.work_state = "explicit_none"
                res.work_evidence = f"简历写明「{hit}」"
            elif res.education and "教育经历" in full_text:
                # 教育区读到了（含滚到底补采），仍然没有任何工作条目 -> 确认无经历
                res.work_state = "explicit_none"
                res.work_evidence = ("整份简历读到教育区，未发现任何工作经历条目"
                                     + ("（含底部补采）" if len(crops) >= max_screens else ""))
        except Exception:
            pass
    res.shots = [(i, c) for i, c in enumerate(crops)]
    res.stopped_reason = "快速读法完成"
    log(f"  结果：工作 {len(res.work)} 条 / 教育 {len(res.education)} 条")
    return res

def _heuristic_work_entries(text: str) -> list[dict]:
    """结构化没读出工作经历时，按行硬捞一遍（公司名 + 职位 + 起止）。"""
    import re as _re
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    entries: list[dict] = []
    cur = None
    in_edu = False
    for ln in lines:
        # 教育区之后的行不当作工作经历
        if _re.match(r"^(教育经历|教育背景|在校经历|校园经历|项目经历|专业技能|资格证书|所获荣誉|自我评价)", ln):
            if _re.match(r"^教育", ln) or _re.match(r"^(项目|专业|资格|所获|自我)", ln):
                in_edu = True
            continue
        if _re.match(r"^工作经历", ln):
            in_edu = False
            continue
        if in_edu:
            continue
        if not _re.search(r"(19|20)\d{2}", ln):
            if cur is not None and len(ln) > 4:
                cur["descriptions"].append(ln)
            continue
        # 带年份的行：可能是"公司 职位 时间"
        m = _re.search(r"((?:19|20)\d{2}[.\-/年]?\d{0,2}\s*[-~至]\s*(?:(?:19|20)\d{2}[.\-/年]?\d{0,2}|至今))", ln)
        period = m.group(1) if m else ""
        head = (ln[:m.start()] if m else ln).strip(" |·-—:：	")
        if not head:
            continue
        if not any(k in head for k in ("公司", "集团", "工作室", "事务所", "中心", "机构",
                                       "医院", "政府", "店", "厂", "传媒", "银行")):
            continue
        parts = [p for p in _re.split(r"[|丨\s·]+", head) if p]
        company = parts[0] if parts else head
        title = parts[1] if len(parts) > 1 else ""
        cur = {"company": company[:30], "title": title[:20], "period": period,
               "descriptions": []}
        entries.append(cur)
    return [e for e in entries if e["descriptions"] or e["period"]]

def _basic_from_text(text: str) -> dict:
    """从 OCR 全文里抓年龄、学习形式、求职状态（比模型稳定）。"""
    out: dict = {}
    if not text:
        return out
    m = __import__("re").search(r"([1-9]\d?)\s*岁", text)
    if m:
        age = int(m.group(1))
        if 14 <= age <= 70:
            out["age"] = age
    # 非全日制有明确写法；没有这些词时按全日制认定（绝大多数统招简历不标注）
    NON_FULL = ("非全日制", "自考", "成人教育", "成人高考", "函授", "网络教育",
                "业余", "脱产", "开放大学", "电大")
    hit = next((kw for kw in NON_FULL if kw in text), "")
    if hit:
        out["education_mode"] = "非全日制"
        out["education_mode_source"] = f"简历标注「{hit}」"
    elif "全日制" in text or "统招" in text:
        out["education_mode"] = "全日制"
        out["education_mode_source"] = "简历标注全日制"
    else:
        out["education_mode"] = "全日制"
        out["education_mode_source"] = "简历未标注，按默认全日制认定"
    m = __import__("re").search(r"(离职|在职|在校)[-－·\s]*(随时到岗|月内到岗|考虑机会|看看机会)?", text)
    if m:
        out["status"] = (m.group(0) or "").strip()
    return out

def _fill_company_from_text(work: list[dict], texts: list[str], log=print) -> int:
    """公司名缺失时，从 OCR 原文里把"公司名 + 职位"那一行捞回来。

    结构化模型偶尔会漏掉公司名（尤其「XX有限公司」和职位挤在一行时），
    而公司名就在原文里，按职位名反查那一行即可。
    """
    if not work or not texts:
        return 0
    joined = "\n".join(texts)
    lines = [ln.strip() for ln in joined.splitlines() if ln.strip()]
    fixed = 0
    for item in work:
        if (item.get("company") or "").strip():
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        for ln in lines:
            if title not in ln:
                continue
            head = ln.split(title)[0].strip(" |·-—:：\t")
            head = head.replace("工作经历", "").strip()
            if len(head) < 3:
                continue
            # 只接受"像公司名"的前缀：以公司/集团/厂/店/中心/学校/医院 结尾，或含"有限公司"
            if not any(k in head for k in ("公司", "集团", "工作室", "事务所", "中心",
                                           "学校", "学院", "医院", "政府", "合作社", "厂")):
                continue
            item["company"] = head[:40]
            fixed += 1
            log(f"    公司名从原文补回：{item['company']}（{title}）")
            break
    return fixed

def _verify_empty_work_block(crop) -> tuple[bool, str]:
    """回看截图确认「工作经历」标题下方到下一个标题之间确实没有文字。

    只看视觉模型输出的文字不可靠（它可能整块漏读），所以用本地 OCR（带坐标）
    复核一遍。任何不确定的情况都返回 False，让调用方保持 unknown。
    """
    if crop is None:
        return False, "没有对应截图，保持 unknown"
    try:
        engine = vision.load_ocr()
        if engine is None:
            return False, "本地 OCR 不可用，保持 unknown"
        items = vision.ocr_crop(engine, crop, vision.Rect(0, 0, crop.shape[1], crop.shape[0]))
        rows = sorted(items, key=lambda it: (it["box"][1], it["box"][0]))
        heads = ("教育经历", "项目经历", "校园经历", "专业技能", "资格证书",
                 "所获荣誉", "自我评价", "工作经历", "实习经历")
        idx_work = None
        for i, it in enumerate(rows):
            if (it.get("text") or "").strip().startswith(("工作经历", "实习经历")):
                idx_work = i
                break
        if idx_work is None:
            return False, "截图里没找到「工作经历」标题，保持 unknown"
        idx_next = None
        for j in range(idx_work + 1, len(rows)):
            if (rows[j].get("text") or "").strip().startswith(heads):
                idx_next = j
                break
        if idx_next is None:
            return False, "「工作经历」后面没找到下一个区块标题，保持 unknown"
        between = [r for r in rows[idx_work + 1: idx_next] if (r.get("text") or "").strip()]
        if between:
            return False, f"标题之间有 {len(between)} 行内容，保持 unknown"
        return True, "标题之间确认为空"
    except Exception as exc:
        return False, f"截图复核失败（{exc}），保持 unknown"


def _merge_usage(a: dict | None, b: dict | None) -> dict:
    """合并两次调用的 token 用量（OCR + 结构化）。"""
    a = a or {}
    b = b or {}
    total = int(a.get("total_tokens") or 0) + int(b.get("total_tokens") or 0)
    return {
        "prompt_tokens": int(a.get("prompt_tokens") or 0) + int(b.get("prompt_tokens") or 0),
        "completion_tokens": int(a.get("completion_tokens") or 0)
                              + int(b.get("completion_tokens") or 0),
        "total_tokens": total,
    }

def _school_name_candidates(crop) -> list[str]:
    """在教育屏截图里用 OCR 找"像校名"的文本，并只保留名单内的。"""
    try:
        import schools as _schools
        engine = vision.load_ocr()
        if engine is None:
            return []
        items = vision.ocr_crop(engine, crop, vision.Rect(0, 0, crop.shape[1], crop.shape[0]))
        out: list[str] = []
        for it in items:
            text = (it.get("text") or "").strip()
            if len(text) < 4 or not any(k in text for k in ("大学", "学院", "学校")):
                continue
            # OCR 常把学校/专业/学历合并成一行（"XX学院丨数字媒体丨大专"），先拆再试
            parts = [x for x in re.split(r"[丨|｜/／、,，\s]+", text) if len(x) >= 4]
            parts.append(text)
            for part in parts:
                if not any(k in part for k in ("大学", "学院", "学校")):
                    continue
                hit = _schools.lookup(part)
                if hit.get("listed") and hit.get("matched"):
                    if hit["matched"] not in out:
                        out.append(hit["matched"])
                    continue
                # 前缀尝试：从长到短截断，找到名单内的校名
                for cut in range(len(part), 3, -1):
                    head = part[:cut]
                    if not any(k in head for k in ("大学", "学院", "学校")):
                        continue
                    h2 = _schools.lookup(head)
                    if h2.get("listed") and h2.get("matched"):
                        if h2["matched"] not in out:
                            out.append(h2["matched"])
                        break
        return out
    except Exception:
        return []


def _verify_school_name(crop, vlm_school: str, cfg: dict, log=print) -> tuple[str, str]:
    """学校名二次校验：VLM 与 OCR 不一致时，再用放大重读投票。

    返回 (最终校名, 说明)。实测 VLM 会把「长江职业学院」读成「长沙职业技术学院」，
    两所都是真学校，只能靠字符级 OCR + 放大重读投票来纠正。
    """
    import difflib

    import vlm as _vlm          # 局部导入，避免模块级循环依赖

    cands = _school_name_candidates(crop)
    if not cands:
        return vlm_school, ""
    best = max(cands, key=lambda c: difflib.SequenceMatcher(None, c, vlm_school).ratio())
    if best == vlm_school:
        return vlm_school, ""

    sim = difflib.SequenceMatcher(None, best, vlm_school).ratio()
    if sim < 0.45:
        return vlm_school, ""          # 差太多，可能是另一段经历，不乱改

    # 两票不一致 -> 放大重读教育区再投一次
    zoom = dict(cfg)
    zoom["max_image_side"] = int(cfg.get("max_image_side", 1024)) * 2
    votes = [vlm_school, best]
    try:
        raw = _vlm.read_screen(crop, zoom, log=lambda *_: None)
        edu_zoom = _vlm.normalize(raw.get("sections") or {})
        zoom_school = ""
        if edu_zoom.get("education"):
            zoom_school = (edu_zoom["education"][0].get("school") or "").strip()
        if zoom_school:
            votes.append(zoom_school)
    except Exception as exc:
        log(f"    [warn] 校名复核重读失败：{exc}")

    # 平票时优先采纳 OCR（逐字读取比整屏理解更可靠）
    counted = [(v, votes.count(v)) for v in dict.fromkeys(votes)]
    counted.sort(key=lambda item: (item[1], item[0] == best), reverse=True)
    final = counted[0][0]
    if final != vlm_school:
        log(f"  校名二次校验：VLM={vlm_school} / OCR={best} → 采用 {final}"
            f"（票数 {votes.count(final)}/{len(votes)}）")
        return final, f"校名已纠正：原读作 {vlm_school}"
    return vlm_school, ""

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
