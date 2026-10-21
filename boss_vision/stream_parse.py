"""带状态的跨屏解析器。

问题背景：BOSS 简历弹窗要逐屏滚动读取，一屏可能只拍到某段经历的后半截，下一屏
才出现公司名；「工作经历」标题滚出屏幕后，后续内容就没有章节归属了。逐屏独立解析
再按 (公司, 时间) 硬合并，会把真实经历切碎或误删。

这里的做法是维护一个**流状态**：

    current_section  当前章节（工作经历 / 项目经历 / 教育经历 …）
    current_entry    正在读的那一条经历
    tail             上一屏末尾没写完的一行（跨屏续行）

规则（与业务约定一致）：

    · 遇到新章节标题      -> 结束当前条目，切换章节
    · 遇到新经历表头      -> 结束当前条目，新建条目
    · 没有表头的后续文本  -> 追加到当前条目（必要时与上一行拼接续行）
    · 同一段经历在下一屏再次出现 -> 合并缺失字段 + 追加新增职责，不整体丢弃

去重只做两件事：
    · 条目内文本去重（相似度）
    · 相邻屏幕重叠区的行去重（由调用方标记 overlap=True 的屏幕）
不同公司的相同职责文本不会被误删。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import extract

SECTIONS = {
    "工作经历": "work",
    "实习经历": "work",
    "社团经历": "campus",
    "在校经历": "campus",
    "校园经历": "campus",
    "项目经历": "project",
    "实习经历": "work",
    "项目经历": "project",
    "校园经历": "campus",
    "在校经历": "campus",
    "教育经历": "edu",
    "资格证书": "cert",
    "专业技能": "skill",
    "自我评价": "summary",
    "求职意向": "intent",
    "荣誉奖项": "honor",
    "所获荣誉": "honor",
}

# 以这些符号开头的行按"新的一条职责"处理，不与上一行拼接
BULLET_STARTS = ("·", "•", "●", "▪", "■", "口", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
                 "-", "—", "*", "（", "(")
END_PUNCT = ("。", "！", "？", "；", ";", "：", ":")
TAG_MAX_LEN = 12


TITLE_WORDS = ("运营", "助理", "专员", "教师", "老师", "摄影", "摄像", "剪辑", "客服", "销售",
               "设计", "编辑", "主管", "经理", "前台", "导购", "工程师", "实习", "编导", "美工",
               "店长", "主播", "中控", "场控", "助播", "文案", "文员", "会计", "出纳", "助教")


def _core_name(text: str) -> str:
    """比对公司名时去掉粘在后面的职位词：小友酒庄销售专员 -> 小友酒庄。"""
    t = (text or "").strip()
    for word in sorted(TITLE_WORDS, key=len, reverse=True):
        if t.endswith(word) and len(t) > len(word) + 1:
            t = t[: -len(word)]
            break
    return t.strip(" 丨|·-—")


def _similar(a: str, b: str, threshold: float = 0.86) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > 8:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def _norm_period(period: str) -> str:
    return re.sub(r"[\s　]", "", period or "")


DATE_TOKEN = re.compile(r"\d{4}(?:[./-]\d{1,2})?")


def _split_dates(text: str) -> tuple[str, tuple]:
    """把时间片段抠出来：返回（去掉时间的正文, 时间元组）。

    比较"是不是同一行重复"时只看正文 —— 否则「甲公司 运营 2024.01-2024.06」和
    「乙公司 运营 2024.01-2024.06」会被当成重复而丢掉一家真实公司。
    """
    dates = tuple(DATE_TOKEN.findall(text or ""))
    body = DATE_TOKEN.sub("", text or "")
    body = re.sub(r"[\s　丨|·\-—至到]+", "", body)
    return body, dates


def _near_dup_any(text: str, recent: list[str], threshold: float = 0.9) -> bool:
    """和最近几屏的行"几乎一样"就当作重复（正文相同且时间相同）。"""
    t = (text or "").strip()
    if not t:
        return True
    body, dates = _split_dates(t)
    if not body:
        return False                       # 纯时间行不做跨屏判重
    for old in recent:
        old_body, old_dates = _split_dates(old)
        if not old_body or old_dates != dates:
            continue
        if abs(len(old_body) - len(body)) > 6:
            continue
        if _similar(old_body, body, threshold):
            return True
    return False



ORG_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团", "公司", "工作室",
                "事务所", "研究院", "中心", "门店", "店", "厂", "社")


def _split_org(head: str) -> tuple[str, str]:
    """把「迪盾控股集团有限公司 国内电商运营」切成 (公司, 职位)。

    取**最靠后的机构后缀**作为公司结尾，避免「…集团有限公司」被切在「集团」上。
    """
    t = head.strip()
    best_end = -1
    for suffix in ORG_SUFFIXES:
        pos = t.rfind(suffix)
        if pos >= 0:
            end = pos + len(suffix)
            if end > best_end:
                best_end = end
    if best_end > 0:
        company = t[:best_end].strip(" 丨|·-—")
        title = t[best_end:].strip(" 丨|·-—")
        return company, title
    parts = re.split(r"[丨|]", t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    tokens = extract._tokenize(t)
    if len(tokens) >= 2:
        return tokens[0], " ".join(tokens[1:])
    return t, ""


def section_of(text: str) -> str | None:
    """这一行是不是章节标题。"""
    t = (text or "").strip().strip("丨|·-— ")
    for head, kind in SECTIONS.items():
        if t == head or (t.startswith(head) and len(t) <= len(head) + 6):
            return kind
    return None


@dataclass
class StreamEntry:
    section: str = "work"
    company: str = ""
    title: str = ""
    period: str = ""
    employment: str = ""
    school: str = ""
    major: str = ""
    degree: str = ""
    descriptions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    screens: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def key(self) -> tuple:
        name = (self.company or self.school or "").strip()
        return (self.section, name, _norm_period(self.period))

    def is_empty(self) -> bool:
        return not any([self.company, self.school, self.title, self.period,
                        self.descriptions, self.tags])


class StreamParser:
    """把逐屏 OCR 行喂进来，得到跨屏连续的条目列表。"""

    def __init__(self) -> None:
        self.section: str | None = None
        self.entries: list[StreamEntry] = []
        self.current: StreamEntry | None = None
        self.pending: list[str] = []      # 可能是表头一部分的行（公司/职位各占一行）
        self.tail: str = ""              # 上一行没写完的文本
        self.tail_screen: int = -1
        self.section_seen: list[str] = []
        self.conflicts: list[str] = []          # 跨屏冲突：时间段相同但公司不同、字段矛盾等
        self._recent: list[str] = []            # 上一屏的行，用于跨屏重叠去重
        self._seen: set[str] = set()

    # ---------- 对外 ----------

    def feed(self, items: list[dict], screen: int, overlap: bool = False) -> None:
        """喂一屏 OCR 结果（items 需带 text / box）。

        先按纵坐标把 OCR 框聚成"视觉行"，再把同一行从左到右拼成一句话 ——
        公司名、职位、右对齐的时间常在同一行但被 OCR 拆成几个框。
        """
        fresh_pieces: list[list[str]] = []
        for pieces in self._rows(items):
            if not pieces:
                continue
            text = " ".join(pieces)
            # 同一行在相邻屏重复出现（页面没滚动/滚动不足）-> 丢掉，否则章节状态会把它归错类
            if text in self._seen or _near_dup_any(text, self._recent):
                continue
            fresh_pieces.append(pieces)

        self._seen.update(" ".join(p) for p in fresh_pieces)
        self._recent = [" ".join(p) for p in fresh_pieces][-80:]

        for pieces in fresh_pieces:
            # 一行里可能混着小节标题（「工作经历」在左边距，第一条经历在同一行右侧）
            heads = [p for p in pieces if section_of(p)]
            if heads:
                for head in heads:
                    self._feed_line(head, screen, overlap)
                rest = [p for i, p in enumerate(pieces) if p not in heads or pieces.index(p) != i]
                rest = [p for p in pieces if not section_of(p)]
                if rest:
                    self._feed_line(" ".join(rest), screen, overlap)
            else:
                self._feed_line(" ".join(pieces), screen, overlap)

    @staticmethod
    def _rows(items: list[dict], tol: int = 10) -> list[list[str]]:
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
        out = []
        for row in rows:
            row.sort(key=lambda i: i["box"][0])
            out.append([i["text"].strip() for i in row if i["text"].strip()])
        return out

    def close(self) -> None:
        """收尾：把当前条目落盘。"""
        self._flush_pending(self.tail_screen)
        self._commit()
        self.tail = ""

    def _all_entries(self) -> list[StreamEntry]:
        """已提交条目 + 还在读取中的当前条目（逐屏读的时候当前条目也应有输出）。"""
        out = list(self.entries)
        if self.current is not None and not self.current.is_empty():
            out.append(self.current)
        return out

    @property
    def work(self) -> list[dict]:
        out = []
        for e in self._all_entries():
            if e.section not in ("work", "project", "campus"):
                continue
            if not (e.company or e.title or e.period or e.descriptions):
                continue                      # 空条目（占位后没拿到任何内容）
            if e.company and any(ch in e.company for ch in "。；，"):
                e.title = (e.company + " " + e.title).strip()   # 公司名里带句读 -> 其实是职责/项目行
                e.company = ""
            out.append(self._as_work(e))
        return out

    @property
    def education(self) -> list[dict]:
        """教育条目出口统一清洗：过滤公司样条目、把纯学历词当学历。"""
        out = []
        for e in self._all_entries():
            if e.section != "edu":
                continue
            name = (e.school or "").strip()
            if name and ("丨" in name or re.search(r"(公司|集团|工作室|事务所|门店|店|厂|中心)$", name)):
                continue                     # 教育章节里混进来的工作条目，直接丢掉
            if name and extract.degree_level_only(name):
                e.degree = e.degree or name  # 「初中及以下」这种是学历不是学校
                e.school = ""
            if not (e.school or e.degree or e.period):
                continue
            out.append(self._as_edu(e))
        return out

    # ---------- 内部 ----------

    def _feed_line(self, text: str, screen: int, overlap: bool) -> None:
        kind = section_of(text)
        if kind:
            self._flush_pending(screen)
            self._commit()
            self.section = kind
            if kind not in self.section_seen:
                self.section_seen.append(kind)
            return

        if self.section is None:
            return                      # 章节之前的身份/摘要信息，不进条目

        if self.section in ("cert", "skill", "summary", "intent", "honor"):
            return                      # 这些章节暂不结构化

        # 1) 完整表头（公司/职位/时间在同一行）
        header = self._as_header(text)
        if header is not None:
            self.pending = []
            self._commit()
            self.current = StreamEntry(section=self.section, screens=[screen], **header)
            self.tail = ""
            return

        # 2) 只有一行的日期：可能是被裁掉表头的那一段，也可能给 pending 里的公司收尾
        if extract.DATE_RE.fullmatch(text.replace(" ", "")) or (
                extract.DATE_RE.search(text) and not extract.DATE_RE.sub("", text).strip(" 丨|·-—")):
            period = extract.DATE_RE.search(text).group(0).strip()
            if self.pending:
                head = " ".join(self.pending)
                self.pending = []
                fields = self._split_header(head, period)
                self._commit()
                self.current = StreamEntry(section=self.section, screens=[screen], **fields)
                return
            if self.current is not None and not self.current.period:
                self.current.period = period
                if screen not in self.current.screens:
                    self.current.screens.append(screen)
                return
            if self.current is None:
                self.current = StreamEntry(section=self.section, period=period,
                                           screens=[screen],
                                           notes=["表头被裁切：只读到时间，公司/职位缺失"])
                return

        # 3) 可能是表头的一部分（公司一行、职位一行），先攒着等时间行
        if self.pending and len(self.pending) < 2 and self._could_be_title(text):
            self.pending.append(text)          # 公司下面紧跟的一行短文本 -> 当成职位
            return
        if self._looks_like_header_part(text):
            if len(self.pending) < 2:
                self.pending.append(text)
                return
            self._flush_pending(screen)

        self._flush_pending(screen)            # 攒着的行没等到时间 -> 当内容
        self._append_content(text, screen, overlap)

    def _could_be_title(self, text: str) -> bool:
        t = text.strip(" 丨|·-—")
        if not t or len(t) > 20:
            return False
        if any(ch in t for ch in "。；，,;：:"):
            return False
        if extract.DATE_RE.search(t) or t.startswith(BULLET_STARTS):
            return False
        return bool(re.search(r"[\u4e00-\u9fa5A-Za-z]", t))

    def _looks_like_header_part(self, text: str) -> bool:
        t = text.strip(" 丨|·-—")
        if not t or len(t) > 30:
            return False
        if any(ch in t for ch in "。；，,;：:"):
            return False
        if t.startswith(BULLET_STARTS):
            return False
        if "丨" in t or "|" in t:      # 「公司丨职位」但时间在下一行
            return True
        return bool(re.search(r"(公司|集团|工作室|事务所|研究院|中心|门店|店|厂|社|部)$", t)) \
            or self._looks_like_title(t)

    @staticmethod
    def _looks_like_title(text: str) -> bool:
        return bool(re.search(r"(运营|助理|专员|教师|老师|摄影|摄像|剪辑|客服|销售|设计|编辑|"
                              r"主管|经理|前台|导购|工程师|实习|编导|美工|店长|主播|中控|场控|"
                              r"助播|文案)$", text.strip()))

    def _flush_pending(self, screen: int) -> None:
        """攒着的行没等到时间行 -> 当普通内容处理，别丢。"""
        if not self.pending:
            return
        for line in self.pending:
            self._append_content(line, screen, overlap=False)
        self.pending = []

    def _split_header(self, head: str, period: str) -> dict:
        if self.section == "edu":
            school, major, degree = extract.split_edu_token(head)
            # 教育章节里出现「XX公司丨职位」说明这一屏其实还是工作经历（章节回跳）
            if re.search(r"(公司|集团|工作室|事务所|门店|店|厂)$", school) or "丨" in head:
                self.conflicts.append(f"教育章节里出现公司样条目：{head[:24]}（按工作经历处理）")
                self.section = "work"
                company, title = _split_org(head)
                return {"company": company, "title": title, "period": period}
            level = extract.degree_level_only(school) if school else False
            if level:
                return {"school": "", "major": "", "degree": school, "period": period}
            return {"school": school, "major": major, "degree": degree, "period": period}
        company, title = _split_org(head)
        return {"company": company, "title": title, "period": period}

    def _as_header(self, text: str) -> dict | None:
        """判断这一行是不是新的经历表头，返回解析出的字段。"""
        if self.section == "edu":
            if not extract.DATE_RE.search(text):
                return None
            body = extract.DATE_RE.sub("", text).strip(" 丨|·-—")
            if not body or len(body) > 40:
                return None
            school, major, degree = extract.split_edu_token(body)
            period = extract.DATE_RE.search(text).group(0).strip()
            return {"school": school, "major": major, "degree": degree, "period": period}

        m = extract.DATE_RE.search(text)
        if not m:
            return None
        head = text[: m.start()].strip()
        tail = text[m.end():].strip()
        if not head or len(head) > 40:
            return None
        if head.endswith(("。", "；", "，", "、", "：", ":", "（", "(", "【", "[")):
            return None
        if head.startswith(BULLET_STARTS):
            return None
        if tail.strip(" ）)]】"):
            return None

        company, title = _split_org(head)
        if not company and "丨" not in head and "|" not in head:
            tokens = extract._tokenize(head)
            if len(tokens) >= 2:
                company, title = tokens[0], " ".join(tokens[1:])
            else:
                company = head
        return {"company": company, "title": title, "period": m.group(0).strip()}

    def _append_content(self, text: str, screen: int, overlap: bool) -> None:
        if self.current is None:
            # 章节刚开始、还没见到表头：先建一个占位条目，避免内容丢失
            self.current = StreamEntry(section=self.section or "work", screens=[screen])
            self.current.notes.append("该条目未读到表头（公司/职位缺失）")

        entry = self.current
        if screen not in entry.screens:
            entry.screens.append(screen)

        # 跨屏续行：上一行没以句读结尾，且该行不是新的要点 -> 拼起来
        if (self.tail and self.tail_screen != screen and not text.startswith(BULLET_STARTS)
                and entry.descriptions and not entry.descriptions[-1].endswith(END_PUNCT)):
            merged = entry.descriptions[-1] + text
            entry.descriptions[-1] = merged
            self.tail = merged if not merged.endswith(END_PUNCT) else ""
            self.tail_screen = screen
            return

        if self._is_tag(text, entry):
            if not any(_similar(text, t) for t in entry.tags):
                entry.tags.append(text)
            return

        for old in entry.descriptions:          # 条目内去重
            if _similar(text, old):
                return
        entry.descriptions.append(text)
        self.tail = text if not text.endswith(END_PUNCT) else ""
        self.tail_screen = screen

    def _is_tag(self, text: str, entry: StreamEntry) -> bool:
        """短、无标点、且已经有多行描述 -> 视为标签。"""
        t = text.strip(" ·•●▪■口-—")
        if len(t) > TAG_MAX_LEN:
            return False
        if any(ch in t for ch in "。，；：,;"):
            return False
        return bool(entry.descriptions)

    def _commit(self) -> None:
        if self.current and not self.current.is_empty():
            self._merge_into_entries(self.current)
        self.current = None

    def _merge_into_entries(self, entry: StreamEntry) -> None:
        """同一段经历再次出现时合并字段与新增职责，而不是整体丢弃。"""
        for existing in self.entries:
            if existing.section != entry.section:
                continue
            same_name = _similar(_core_name(existing.company or existing.school),
                                 _core_name(entry.company or entry.school), threshold=0.8)
            same_period = _norm_period(existing.period) == _norm_period(entry.period)
            missing_name = not (existing.company or existing.school) or not (entry.company or entry.school)
            shared_text = any(_similar(a, b)
                              for a in existing.descriptions for b in entry.descriptions)

            both_named = bool(existing.company) and bool(entry.company)
            if both_named and same_period and not same_name:
                self.conflicts.append(
                    f"同一时间段 {existing.period} 出现两家公司：{existing.company} / {entry.company}")
                continue                        # 不合并，各自保留

            if same_name and (same_period or not entry.period or not existing.period):
                # 职位/公司字段矛盾时保留先读到的值，并记冲突
                for attr, label in (("title", "职位"), ("company", "公司")):
                    a, b = getattr(existing, attr), getattr(entry, attr)
                    if a and b and not _similar(a, b, 0.7):
                        self.conflicts.append(f"{existing.period} {label}不一致：{a} / {b}（保留前者）")
                self._absorb(existing, entry)
                return
            # 一屏只拍到时间+职责、另一屏才拍到表头：靠职责重叠认定是同一条
            if missing_name and (same_period or shared_text) and (shared_text or same_period):
                self._absorb(existing, entry)
                return
        self.entries.append(entry)

    @staticmethod
    def _absorb(target: StreamEntry, source: StreamEntry) -> None:
        for attr in ("company", "title", "period", "employment", "school", "major", "degree"):
            if not getattr(target, attr) and getattr(source, attr):
                setattr(target, attr, getattr(source, attr))
        for text in source.descriptions:
            if not any(_similar(text, old) for old in target.descriptions):
                target.descriptions.append(text)
        for tag in source.tags:
            if not any(_similar(tag, old) for old in target.tags):
                target.tags.append(tag)
        for s in source.screens:
            if s not in target.screens:
                target.screens.append(s)
        for note in source.notes:
            if note not in target.notes:
                target.notes.append(note)

    @staticmethod
    def _as_work(e: StreamEntry) -> dict:
        return {
            "company": e.company, "title": e.title, "employment": e.employment,
            "period": e.period, "descriptions": e.descriptions, "tags": e.tags,
            "screens": e.screens, "notes": e.notes,
        }

    @staticmethod
    def _as_edu(e: StreamEntry) -> dict:
        return {
            "school": e.school, "major": e.major, "degree": e.degree, "period": e.period,
            "screens": e.screens, "notes": e.notes,
        }
