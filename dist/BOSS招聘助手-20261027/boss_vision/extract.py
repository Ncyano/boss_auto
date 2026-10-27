"""从简历弹窗的 OCR 结果里结构化提取「工作经历 / 教育经历」。

输入是带坐标的 OCR 行，输出是结构化条目，而不是一段拼接文本。

判读规律（来自 5 份真实简历）
--------------------------
* 小节标题独占一行：「工作经历」「教育经历」，下面直到下一个小节标题都属于它。
* 每条经历的第一行是「公司 · 职位 ... 右对齐时间范围」，时间形如
  ``2023.06 - 至今`` / ``2026.03 - 2026.06`` / ``2021 - 2025``。
* 该行之下、下一条经历之前的所有行，是这条经历的描述与标签。
* 教育经历同理，学校名通常带「学院/大学/学校」，学历是「本科/大专/硕士…」。

用法
----
    python extract.py --image runs\\x.png        # 对图片提取
    python extract.py --live                     # 对当前 BOSS 窗口提取
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import vision
import winenv

WORK_HEAD = "工作经历"
EDU_HEAD = "教育经历"
OTHER_HEADS = ("项目经历", "实习经历", "校园经历", "在校经历", "自我评价", "求职意向",
               "资格证书", "所获荣誉", "专业技能", "获奖经历", "培训经历", "牛人分析器")

DATE_RE = re.compile(
    r"(\d{4})\s*[.\-/年]?\s*(\d{1,2})?\s*[月]?\s*[-–—~至]+\s*"
    r"((?:\d{4})\s*[.\-/年]?\s*(?:\d{1,2})?\s*[月]?|至今|现在|今)"
)
SCHOOL_RE = re.compile(r"(学院|大学|学校|职业|技术学院|高中|中专|中学)")
DEGREE_RE = re.compile(r"(本科|大专|专科|硕士|博士|研究生|MBA|EMBA|中专|高中|初中|学士|学历不限)")
INTERN_RE = re.compile(r"(实习|全职|兼职|应届)")
# 公司名常见后缀，用来把「公司」和「职位」分开
SEP_RE = re.compile(r"[丨|｜]")
COMPANY_RE = re.compile(
    r"(公司|有限|集团|工作室|学院|大学|学校|中心|门店|分店|店|社|台|局|厂|部|所|行|馆|院|局|处)$"
)


def panel_filter(lines: list[dict], wide: vision.Rect | None) -> list[dict]:
    """只保留弹窗正文区的行，排除左侧导航与右侧侧栏。

    有「打招呼」大按钮时按它的锚点算正文左右边界；没有（比如只截了正文的图）就全留。
    """
    if wide is None:
        return lines
    left = wide.x - 809   # 实测：按钮 x=1609 时正文左边界 800
    right = wide.x - 35   # 正文右边界（侧栏左边）
    kept = []
    for item in lines:
        x0, _, x1, _ = item["box"]
        if x0 >= left - 30 and x1 <= right + 30:
            kept.append(item)
    return kept


def split_label_rows(rows: list[list[dict]]) -> list[list[dict]]:
    """小节标题常和第一条经历挤在同一视觉行，这里把标题单独拆出来。"""
    heads = (WORK_HEAD, EDU_HEAD, *OTHER_HEADS)
    out: list[list[dict]] = []
    for row in rows:
        if len(row) > 1:
            first = row[0]["text"].strip()
            if any(first.startswith(h) for h in heads):
                out.append([row[0]])
                out.append(row[1:])
                continue
        out.append(row)
    return out


def merge_date_only_rows(rows: list[list[dict]]) -> list[list[dict]]:
    """把只有时间、没有其他文字的行并进上一行（时间有时会被单独识别出来）。"""
    out: list[list[dict]] = []
    for row in rows:
        text = row_text(row).strip()
        if out and text and DATE_RE.fullmatch(text.replace(" ", "")):
            out[-1] = out[-1] + row
            continue
        out.append(row)
    return out


@dataclass
class Entry:
    company: str = ""
    title: str = ""
    employment: str = ""
    period: str = ""
    descriptions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw_rows: list[str] = field(default_factory=list)


@dataclass
class Education:
    school: str = ""
    major: str = ""
    degree: str = ""
    period: str = ""
    raw_rows: list[str] = field(default_factory=list)


@dataclass
class Resume:
    work: list[Entry] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    other_sections: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "work": [asdict(w) for w in self.work],
            "education": [asdict(e) for e in self.education],
            "sections_found": self.sections_found,
            "other_sections": self.other_sections,
        }


def group_rows(lines: list[dict], y_tol: int = 12) -> list[list[dict]]:
    """把 OCR 行按 y 聚成"视觉行"（同一行的公司名/职位/时间会分别被识别成多个框）。"""
    rows: list[list[dict]] = []
    for item in sorted(lines, key=lambda d: (d["box"][1], d["box"][0])):
        yc = (item["box"][1] + item["box"][3]) / 2
        if rows:
            last = rows[-1]
            last_yc = sum((i["box"][1] + i["box"][3]) / 2 for i in last) / len(last)
            if abs(yc - last_yc) <= y_tol:
                last.append(item)
                continue
        rows.append([item])
    for row in rows:
        row.sort(key=lambda d: d["box"][0])
    return rows


def row_text(row: list[dict]) -> str:
    return " ".join(i["text"].strip() for i in row)


def find_sections(rows: list[list[dict]]) -> list[tuple[str, int]]:
    """返回 [(小节名, 起始行号)]，按行号升序。"""
    found: list[tuple[str, int]] = []
    for i, row in enumerate(rows):
        t = row_text(row).replace(" ", "")
        if not t or len(t) > 12:
            continue
        if t.startswith(WORK_HEAD):
            found.append((WORK_HEAD, i))
        elif t.startswith(EDU_HEAD):
            found.append((EDU_HEAD, i))
        else:
            for head in OTHER_HEADS:
                if t.startswith(head):
                    found.append((head, i))
                    break
    return found


def section_slice(rows: list[list[dict]], sections: list[tuple[str, int]], name: str):
    """取某个小节的行，直到下一个小节标题为止。"""
    for k, (head, start) in enumerate(sections):
        if head != name:
            continue
        end = sections[k + 1][1] if k + 1 < len(sections) else len(rows)
        return rows[start + 1 : end]
    return []


def split_period(text: str) -> tuple[str, str]:
    """从一行里切出时间范围，返回 (时间, 去掉时间后的剩余文本)。"""
    m = DATE_RE.search(text)
    if not m:
        return "", text
    return m.group(0).strip(), (text[: m.start()] + " " + text[m.end() :]).strip()


def _clean(token: str) -> str:
    """去掉 OCR 常见的行首编号/图标残留。"""
    return re.sub(r"^[\d·•\-\s]+", "", token).strip()


def _tokenize(text: str) -> list[str]:
    """把「福建飞鸟传媒有限公司丨后期剪辑·后期部」这类行切成词。

    先把 丨/|/｜ 当成空格，再按空白切；顺带去掉残留的空段。
    """
    return [_clean(t) for t in re.split(r"\s+", SEP_RE.sub(" ", text).strip()) if _clean(t)]



SCHOOL_TAIL = re.compile(r"^(.{2,25}?(?:职业技术学院|技术学院|职业学院|学院|大学|学校|中学|中专|技校))")
COMPANY_TAIL = re.compile(
    r"^(.{2,30}?(?:股份有限公司|有限责任公司|有限公司|公司|集团|工作室|事务所|研究院|中心|门店|店|厂|社))"
)


def split_edu_token(tok: str) -> tuple[str, str, str]:
    """把「泉州纺织服装职业学院丨网络营销与直播电商丨大专」这类粘连串拆成 (学校, 专业, 学历)。

    已按分隔符切过词的情况下也可能整行只是一个 token，这里再按词边界拆一次。
    """
    t = SEP_RE.sub(" ", tok).strip()
    m = SCHOOL_TAIL.match(t)
    if not m:
        return tok.strip(), "", ""
    school = m.group(1)
    rest = t[m.end():].strip(" 丨|·-")
    degree = ""
    dm = DEGREE_RE.search(rest)
    if dm:
        degree = dm.group(1)
        rest = (rest[: dm.start()] + " " + rest[dm.end():]).strip(" 丨|·-")
    major = rest.strip(" 丨|·-/")
    return school, major, degree


SECTION_WORDS = ("工作经历", "教育经历", "项目经历", "实习经历", "校园经历", "在校经历",
                 "自我评价", "求职意向", "资格证书", "专业技能", "所获荣誉", "荣誉奖项")




def degree_level_only(text: str) -> bool:
    """文本是否只是学历词（用于排除把「本科」误当专业）。"""
    import screening
    return screening.degree_level(text) is not None and len(text) <= 12


def _period_key(period: str) -> tuple:
    """把「2024.07-2025.01」折成可排序的键：是否至今 + 结束年月。"""
    text = period or ""
    current = ("至今" in text) or ("现在" in text)
    tokens = re.findall(r"(\d{4})(?:\.(\d{1,2}))?", text)
    if not tokens:
        return (0, 0, 0)
    year, month = tokens[-1] if len(tokens) >= 2 else tokens[0]
    return (1 if current else 0, int(year), int(month or 0))


def _sort_by_period(entries: list) -> list:
    """有时间就按时间倒序（最近一段在前）；没时间的保持原顺序。"""
    if sum(1 for e in entries if (getattr(e, "period", "") or "").strip()) < 2:
        return entries
    return sorted(entries, key=lambda e: _period_key(getattr(e, "period", "")), reverse=True)



def _clean_entries(entries: list) -> list:
    """清掉明显不对的条目：空条目、以标点开头的"公司"、像整段正文的公司名。"""
    out = []
    for e in entries:
        name = (getattr(e, "company", "") or getattr(e, "school", "")).strip()
        period = (getattr(e, "period", "") or "").strip()
        descs = getattr(e, "descriptions", []) or []
        if not name and not descs:
            continue
        if name and name[0] in "、。，；：:·.·-■口▪)）】]":
            continue
        if len(name) > 30 or any(ch in name for ch in "。；"):
            continue
        out.append(e)
    return out


def _similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """两个名称是不是"同一家"（OCR 两次识别常有细微差异）。"""
    import difflib

    if not a or not b:
        return a == b
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


SCHOOL_HINT = ("学校", "学院", "大学", "中学", "职校", "技校", "师范", "职业技术")
ORG_HINT = re.compile(r"(公司|集团|有限|工作室|门店|店|中心|学院|大学|学校|医院|银行|协会|事务所|工厂|厂|研究院|馆|局|社|台|事务所)")
TITLE_WORDS = ("运营", "助理", "专员", "教师", "老师", "摄影", "摄像", "剪辑", "客服", "销售",
               "设计", "编辑", "主管", "经理", "前台", "导购", "teacher", "工程师", "实习生", "导演",
               "拍摄", "新媒体", "直播", "中控", "场控", "助播", "文案", "编导", "美工", "店长",
               "主播", "学徒", "店员", "文员", "会计", "出纳")


def _looks_like_title(text: str) -> bool:
    """没有机构后缀、却带职位关键词 -> 这是职位不是公司。"""
    t = (text or "").strip()
    if not t or ORG_HINT.search(t):
        return False
    return any(w in t for w in TITLE_WORDS)


# 只有城市名没有公司名（OCR 把公司后半截丢了）时，不能把城市当成公司
CITY_ONLY = {
    "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州", "武汉", "成都",
    "西安", "长沙", "郑州", "青岛", "济南", "合肥", "福州", "厦门", "泉州", "莆田", "宁德",
    "漳州", "龙岩", "三明", "南平", "南昌", "赣州", "宁波", "温州", "嘉兴", "绍兴", "东莞",
    "佛山", "珠海", "中山", "惠州", "汕头", "昆明", "贵阳", "南宁", "海口", "太原", "石家庄",
    "沈阳", "大连", "长春", "哈尔滨", "兰州", "银川", "西宁", "乌鲁木齐", "呼和浩特",
}


def _is_city_only(text: str) -> bool:
    return (text or "").strip(" 丨|·-、") in CITY_ONLY


def _is_section_word(text: str) -> bool:
    """「工作经历」这类小节标题被误当成公司/学校名时的兜底判断。"""
    t = (text or "").strip(" 丨|·:-")
    return any(t.startswith(w) or t == w for w in SECTION_WORDS)


def _strip_tail_junk(tok: str) -> str:
    """去掉 OCR 在名称末尾留下的杂字，如「…有限公司Q」「…有限公司丨1」。"""
    return re.sub(r"[A-Za-z0-9丨|·、]+$", "", tok).strip()


def split_company_title(tok: str) -> tuple[str, str]:
    """把「上海车轮互联网服务有限公司丨社群运营·学员成长部」这类粘连串拆成 (公司, 职位)。"""
    t = SEP_RE.sub(" ", tok).strip()
    m = COMPANY_TAIL.match(t)
    if not m:
        return tok.strip(), ""
    company = m.group(1)
    title = re.sub(r"^[A-Za-z0-9丨|·、\s]+", "", t[m.end():]).strip()
    if len(title) < 2:                 # 「网店店」+「长」这种是拆错了，整体留作职位
        return "", t
    return company, title


def parse_work_entry(header_row: list[dict], body_rows: list[list[dict]]) -> Entry:
    pieces = [i["text"].strip() for i in header_row if i["text"].strip()]
    joined = " ".join(pieces)
    period, rest = split_period(joined)
    emp = ""
    m = INTERN_RE.search(rest)
    if m:
        emp = m.group(1)
        rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()

    tokens = _tokenize(rest)
    company, title = "", ""
    if len(tokens) == 1:
        company, title = split_company_title(tokens[0])
    elif len(tokens) >= 2:
        idx = next((k for k, t in enumerate(tokens) if COMPANY_RE.search(_strip_tail_junk(t))), 0)
        company = _strip_tail_junk(tokens[idx])
        title = " ".join(tokens[:idx] + tokens[idx + 1 :])
        title = re.sub(r"^[A-Za-z0-9丨|·、\s]+", "", title).strip()
    descriptions, tags = [], []
    _skip_headers = True
    for row in body_rows:
        t = row_text(row).strip()
        if not t:
            continue
        if len(t) <= 12 and not t.endswith(("。", "；", "，")) and (" " not in t or t.count(" ") <= 3):
            tags.append(t)
        else:
            descriptions.append(t)
    # OCR 有时把「迪盾控股集团有限公司」拆成两个词，后半截跑进了职位
    if title and ORG_HINT.match(title.strip()) and company:
        company = (company + title).strip()
        title = ""

    # 「金誉面包店店员营业员」这种：机构后缀后面还粘着职位 -> 拆开
    if company and not title:
        m = ORG_HINT.search(company)
        if m:
            rest = company[m.end():].strip()
            if rest and any(w in rest for w in TITLE_WORDS):
                company, title = company[: m.end()], rest

    if _is_section_word(company) or _is_city_only(company):
        # 小节标题 / 只有城市名 -> 公司名缺失，不留脏数据
        if _is_city_only(company) and title:
            title = title
        company = ""
    elif _looks_like_title(company):
        title = (company + " " + title).strip()
        company = ""
    entry = Entry(company=company, title=title, employment=emp, period=period,
                  descriptions=descriptions, tags=tags)
    entry.raw_rows = [joined] + [row_text(r) for r in body_rows]
    return entry


def _fix_education_with_list(edus: list) -> list:
    """用《全国普通高等学校名单》校正学校名：能拆分粘连的"学校+专业"，能过滤明显垃圾。

    名单是权威基准，比任何启发式规则都可靠：
      · 命中名单 -> 用名单里的规范校名
      · 未命中但"某个前缀"命中 -> 该前缀是学校，其余是专业
      · 都不命中且文本很长 -> 判定为解析噪声，学校留空
    """
    try:
        import schools
    except Exception:
        return edus
    if not getattr(schools, "LIST_CSV", None) or not schools.LIST_CSV.exists():
        return edus

    data = schools.load()
    records: dict = data["records"]
    if not records:
        return edus

    for e in edus:
        name = (getattr(e, "school", "") or "").strip()
        if not name:
            continue
        if not getattr(e, "school_raw", ""):
            try:
                e.school_raw = name          # 保留 OCR 原文，校正只改 school
            except Exception:
                pass
        res = schools.lookup(name)
        if res["listed"]:
            e.school = res["matched"] or name
            continue

        norm = schools.normalize(name)
        best = ""
        for cut in range(min(len(norm), 30), 1, -1):
            if norm[:cut] in records:
                best = records[norm[:cut]]["学校名称"]
                break
        if best:
            rest = norm[len(schools.normalize(best)):]
            e.school = best
            if rest and not (getattr(e, "major", "") or "").strip():
                e.major = rest
            continue

        # 对不上名单，且不像校名（没有"学校/学院/大学"这类词）-> 多半是专业或噪声
        if len(norm) > 20 or not any(w in norm for w in SCHOOL_HINT):
            if not (getattr(e, "major", "") or "").strip() and norm:
                e.major = norm
            e.school = ""
    return edus


def parse_edu_entry(header_row: list[dict], body_rows: list[list[dict]]) -> Education:
    pieces = [i["text"].strip() for i in header_row if i["text"].strip()]
    joined = " ".join(pieces)
    period, rest = split_period(joined)
    tokens = _tokenize(rest)

    if len(tokens) == 1:
        school, major, degree = split_edu_token(tokens[0])
        if DEGREE_RE.fullmatch(school.strip()):      # 只有一个"本科"这种词，别当成学校
            degree, school = school.strip(), ""
    else:
        school = next((_strip_tail_junk(t) for t in tokens if SCHOOL_RE.search(_strip_tail_junk(t))), "")
        if not school:
            cand = tokens[0] if tokens else ""
            # 首词若是"本科/大专/中专"这类学历词，说明学校那行没被识别出来，宁可留空
            school = "" if DEGREE_RE.fullmatch(cand.strip()) else cand
        degree = next((t for t in tokens
                       if DEGREE_RE.search(t) and len(t) <= 12 and not SCHOOL_RE.search(t)), "")
        if degree:
            word = DEGREE_RE.search(degree).group(1)
            mode = ""
            for m in ("非全日制", "全日制", "自考", "函授", "成人"):
                if m in degree:
                    mode = m
                    break
            degree = f"{word}（{mode}）" if mode and mode != "全日制" else word
        major = next((t for t in tokens if t not in (school, degree) and len(t) >= 2), "")
    if not major or not degree:                 # 版式里专业/学历有时被 OCR 挤到上一行
        body_tokens: list[str] = []
        for row in body_rows[:2]:
            body_tokens.extend(_tokenize(row_text(row)))
        for t in body_tokens:
            if not degree and DEGREE_RE.search(t) and len(t) <= 12:
                degree = t.strip()
                if "（" not in degree:
                    for m in ("非全日制", "全日制", "自考", "函授", "成人"):
                        if m in degree:
                            degree = f"{DEGREE_RE.search(degree).group(1)}（{m}）"
                            break
            if not major and not SCHOOL_RE.search(t) and not DEGREE_RE.fullmatch(t.strip()):
                cleaned = t.strip(" 丨|·-、")
                if 2 <= len(cleaned) <= 12 and not degree_level_only(cleaned):
                    major = cleaned
        if not school:
            for t in body_tokens:
                if SCHOOL_RE.search(t):
                    school = _strip_tail_junk(t)
                    break

    if _is_section_word(school):
        school = ""
    edu = Education(school=school, major=major, degree=degree, period=period)
    edu.raw_rows = [joined] + [row_text(r) for r in body_rows if row_text(r).strip()]
    return edu


def _is_entry_header(row: list[dict]) -> bool:
    return bool(DATE_RE.search(row_text(row)))


def parse_section_rows(rows: list[list[dict]], parser) -> list:
    """把一个小节的行按"含时间范围的行"切成若干条。"""
    entries, current_header, body = [], None, []
    for row in rows:
        if _is_entry_header(row):
            if current_header is not None:
                entries.append(parser(current_header, body))
            current_header, body = row, []
        elif current_header is not None:
            body.append(row)
    if current_header is not None:
        entries.append(parser(current_header, body))
    return entries



def parse_section_text(text: str, kind: str) -> list:
    """从「小节纯文本」里重新解析条目（用于修复历史结果，没有坐标也能解析）。

    BOSS 简历里公司/职位和右对齐的时间经常被 OCR 拆成两行（时间在后），
    所以先把"只有时间"的行并回上一行，再按"含时间的行 = 条目头"切段。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    TAG_ONLY = {"实习", "全职", "兼职", "应届", "在校", "离职", "在职"}
    merged: list[str] = []
    for ln in lines:
        date_only = bool(DATE_RE.fullmatch(ln.replace(" ", "")))
        if date_only and merged:
            if merged[-1].strip() in SECTION_WORDS:      # 别把时间并到小节标题上
                merged.append(ln)
                continue
            prev_tag = merged[-1].strip(" 【】[]()（）·-:：")
            prev = merged[-1]
            prev2 = merged[-2] if len(merged) >= 2 else None
            if prev_tag in TAG_ONLY and len(merged) >= 2:
                tag = merged.pop()                  # "实习"这类标签不该吃掉时间
                merged[-1] = merged[-1] + " " + ln
                merged.append(tag)
            elif (prev2 and ORG_HINT.search(prev2) and not ORG_HINT.search(prev)
                  and 2 <= len(prev) <= 20 and not any(ch in prev for ch in "。；，")):
                # 版式：公司一行、职位一行、时间单独一行 -> 合成「公司丨职位 时间」
                merged.pop()
                merged[-1] = prev2 + "丨" + prev
                merged[-1] = merged[-1] + " " + ln
            else:
                merged[-1] = merged[-1] + " " + ln
        else:
            merged.append(ln)

    def is_header(line: str) -> bool:
        """只有「短前缀 + 行尾时间」才算条目头。

        描述正文里也常出现日期（例如"...2021.09 -2023.02..."），以前会被误判成新条目。
        """
        m = DATE_RE.search(line)
        if not m:
            return False
        if len(line) - m.end() > 14:            # 时间后面还有一长串 -> 不是右对齐的时间
            return False
        head = line[: m.start()].strip()
        tail = line[m.end():].strip()
        if not head or len(head) > 40:          # 前缀太长 -> 是描述正文
            return False
        # 前缀以标点/左括号/冒号结尾 -> 说明时间是句子的一部分，例如「阶段一：xx（2025.07-2026.03)」
        if head.endswith(("。", "；", "，", "、", "：", ":", "（", "(", "【", "[", "·", "-", "～")):
            return False
        if head.startswith(("、", "。", "，", "；", "：", ":", ".", "·", "-", "■", "口", "▪")):
            return False
        if tail.strip(" ）)]】 "):               # 时间后面还有内容 -> 不是右对齐的时间
            return False
        return True

    # 首条经历的表头被裁掉时，只剩一行时间：单独成一条，内容不丢（后面会标需复核）
    first_header = next((i for i, ln in enumerate(merged) if is_header(ln)), None)
    orphan: list | None = None
    if first_header:
        pre = merged[:first_header]
        date_lines = [ln for ln in pre if DATE_RE.search(ln)]
        if date_lines:
            orphan = [date_lines[0], [ln for ln in pre
                                     if ln not in date_lines and ln.strip() not in SECTION_WORDS]]
            merged = merged[first_header:]

    groups: list[list] = []
    cur: list | None = None
    pending: list[str] = []                     # 首条之前的零散行（专业/学历常被挤到上面）
    for ln in merged:
        if is_header(ln):
            if cur:
                groups.append(cur)
            cur = [ln, list(pending)]
            pending = []
        elif cur is not None:
            cur[1].append(ln)
        elif not any(w in ln for w in SECTION_WORDS):
            pending.append(ln)
    if cur:
        groups.append(cur)

    fake = lambda t: {"text": t, "score": 0.9, "box": [0, 0, 1, 1]}  # noqa: E731
    parsed = []
    if orphan and kind == "work":
        parsed.append(parse_work_entry([fake(orphan[0])], [[fake(b)] for b in orphan[1]]))
    for head, body in groups:
        row = [fake(head)]
        brows = [[fake(b)] for b in body]
        parsed.append(parse_work_entry(row, brows) if kind == "work" else parse_edu_entry(row, brows))
    if kind == "edu":
        parsed = _fix_education_with_list(_clean_entries(parsed))

    parsed = _clean_entries(parsed)

    # 相邻帧重复采集会让同一段经历出现两次（OCR 细微差异），这里按「时间 + 相似名称」去重
    out = []
    seen: list[tuple[str, object]] = []
    for item in parsed:
        name = getattr(item, "company", "") or getattr(item, "school", "")
        period = re.sub(r"\s+", "", getattr(item, "period", ""))   # 时间里的空格不算差异
        descs = getattr(item, "descriptions", []) or []
        if name and len(name.strip(" 【】[]()（）·-:：")) < 2:
            continue                                    # 「【」这种垃圾名直接丢
        if not name and not descs:
            continue                                    # 既没名字也没内容 -> 空条目
        dup = False
        for seen_period, seen_name in seen:
            if period == seen_period and _similar(name, seen_name):
                dup = True
                break
            # 同一时间段、其中一条没有公司名：说明是重复采集，优先保留有公司名的那条
            if period and period == seen_period and (not name or not seen_name):
                dup = True
                if name and not seen_name:
                    idx = next(i for i, (p, n) in enumerate(seen)
                               if p == period and n == seen_name)
                    seen[idx] = (period, name)
                    out[idx] = item
                break
        if dup:
            continue
        seen.append((period, name))
        out.append(item)

    if kind == "work":
        out = _sort_by_period(out)      # 时间顺序错乱时（拼接所致）按时间倒序纠正
    return out

def extract(lines: list[dict], wide: vision.Rect | None = None) -> Resume:
    lines = panel_filter(lines, wide)
    rows = group_rows(lines)
    rows = split_label_rows(rows)
    rows = merge_date_only_rows(rows)
    sections = find_sections(rows)
    res = Resume(sections_found=[s[0] for s in sections])

    work_rows = section_slice(rows, sections, WORK_HEAD)
    edu_rows = section_slice(rows, sections, EDU_HEAD)
    res.work = parse_section_rows(work_rows, parse_work_entry)
    res.education = parse_section_rows(edu_rows, parse_edu_entry)

    for head, start in sections:
        if head in (WORK_HEAD, EDU_HEAD):
            continue
        res.other_sections[head] = "\n".join(row_text(r) for r in section_slice(rows, sections, head))
    return res


def lines_from_image(path: str, engine=None) -> tuple[list[dict], vision.Rect | None]:
    img = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1].copy()
    engine = engine or vision.load_ocr()
    if engine is None:
        raise RuntimeError("OCR 未安装")
    wides = vision.wide_buttons(vision.detect_teal(img))
    return vision.ocr_crop(engine, img), (wides[0] if wides else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args()

    winenv.enable_dpi_awareness()
    engine = vision.load_ocr()
    if engine is None:
        print("OCR 未安装")
        return 2

    if a.image:
        lines, wide = lines_from_image(a.image, engine)
        src = a.image
    elif a.live:
        found = winenv.find_window("BOSS直聘")
        if not found:
            print("没找到 BOSS 窗口")
            return 2
        img = winenv.grab_window(found[1])
        lines = vision.ocr_crop(engine, img)
        wides = vision.wide_buttons(vision.detect_teal(img))
        wide = wides[0] if wides else None
        src = "BOSS 窗口"
    else:
        print("需要 --image 或 --live")
        return 2

    res = extract(lines, wide)
    if a.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"来源: {src}；识别 {len(lines)} 行；小节: {res.sections_found}")
    print(f"\n工作经历 {len(res.work)} 条")
    for i, w in enumerate(res.work, 1):
        print(f"  #{i} {w.company} | {w.title} | {w.period} {w.employment}")
        for d in w.descriptions[:3]:
            print(f"       · {d[:60]}")
        if w.tags:
            print(f"       标签: {w.tags[:6]}")
    print(f"\n教育经历 {len(res.education)} 条")
    for i, e in enumerate(res.education, 1):
        print(f"  #{i} {e.school} | {e.major} | {e.degree} | {e.period}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
