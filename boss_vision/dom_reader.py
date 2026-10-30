"""从 DOM 读取简历侧栏（经历概览），作为 OCR 结果的权威骨架。

背景：BOSS 简历正文的职责描述不在 DOM 里（虚拟渲染），但「经历概览」侧栏有
公司 / 时间段 / 职位 / 时长 的完整列表，且零成本、零延迟。

用途：
    · 姓名、公司、时间 以侧栏为准（避免 OCR 读到页面账号名）
    · OCR 只负责补每段的职责描述
"""
from __future__ import annotations

import re
from typing import Any

# 在 document + 所有同源 iframe 里找简历弹窗（含侧栏经历概览）
_JS_READ = r"""
(() => {
  const D = [document];
  for (const f of document.querySelectorAll('iframe')) {
    try { if (f.contentDocument) D.push(f.contentDocument); } catch(e) {}
  }
  const pick = sel => {
    for (const d of D) { const el = d.querySelector(sel); if (el) return el; }
    return null;
  };
  const body = pick('.boss-dialog__body') || pick('[class*=resume-detail]');
  if (!body) return null;
  const txt = (body.innerText || body.textContent || '').replace(/\u00a0/g, ' ');
  // 姓名：弹窗标题区域（头像旁）
  let name = '';
  const nameEl = pick('[class*=resume-name], [class*=geek-name], [class*=name-box]');
  if (nameEl) name = (nameEl.innerText || '').trim().split('\n')[0];
  return {text: txt, name: name};
})()
"""

WORK_RE = re.compile(
    r"(?P<company>[^\n]{2,40}?(?:有限公司|公司|集团|科技|工作室|事务所|中心|商行|门店|医院|学校|学院|银行))"
    r"\s*(?P<start>\d{4}[.\-/]\d{1,2})\s*[-–—]\s*(?P<end>\d{4}[.\-/]\d{1,2}|至今)"
    r"\s*(?P<title>[^\s\d][^\n]{0,24}?)?"
    r"\s*(?P<dur>\d+年(?:\d+个月)?|\d+个月)"
)
EDU_RE = re.compile(
    r"(?P<school>[^\n]{2,30}?(?:大学|学院|学校|职校|技校|中学))"
    r"\s*(?P<start>\d{4})\s*[-–—]\s*(?P<end>\d{4})\s*"
    r"(?P<major>[^\n•·]{2,20})?\s*[•·]\s*(?P<degree>[^\n]{2,10})"
)


def _flatten(text: str) -> str:
    """把多行文本压成单行，便于正则跨行匹配。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


HEADER_WORDS = ("收藏", "转发", "举报", "不合适", "打招呼", "经历概览", "分享", "下载")


def parse_sidebar(text: str) -> dict[str, Any]:
    """解析侧栏文本 → 工作/教育骨架。"""
    flat = _flatten(text)
    # 去掉弹窗顶部的操作按钮文字，否则会被当成第一家公司
    for w in HEADER_WORDS:
        flat = flat.replace(w, " ")
    flat = re.sub(r"\s+", " ", flat).strip()
    works = []
    for m in WORK_RE.finditer(flat):
        works.append({
            "company": m.group("company").strip(),
            "period": f"{m.group('start')} - {m.group('end')}",
            "title": (m.group("title") or "").strip(),
            "duration": m.group("dur"),
            "descriptions": [],          # 侧栏没有职责，留给 OCR 补
        })
    rest = flat
    for m in WORK_RE.finditer(flat):
        rest = rest.replace(m.group(0), " ")
    edus = []
    for m in EDU_RE.finditer(rest):
        edus.append({
            "school": m.group("school").strip(),
            "period": f"{m.group('start')} - {m.group('end')}",
            "major": (m.group("major") or "").strip(),
            "degree": (m.group("degree") or "").strip(),
        })
    return {"work": works, "education": edus, "raw": flat[:400]}


def read(cli) -> dict[str, Any]:
    """从浏览器读取并解析侧栏。失败返回空结构。"""
    empty = {"work": [], "education": [], "name": "", "raw": ""}
    try:
        res = cli._call("Runtime.evaluate",
                        {"expression": _JS_READ, "returnByValue": True})
        data = (res.get("result") or {}).get("value") or {}
    except Exception:
        return empty
    text = data.get("text") or ""
    if not text.strip():
        return empty
    parsed = parse_sidebar(text)
    parsed["name"] = (data.get("name") or "").strip()
    return parsed


def merge(ocr_parts: dict, side: dict) -> dict:
    """以侧栏为骨架，用 OCR 的职责描述填充；差异标记出来。"""
    ocr_work = list((ocr_parts or {}).get("work") or [])
    side_work = list((side or {}).get("work") or [])
    notes: list[str] = []

    def norm(s: str) -> str:
        return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(s or ""))[:8]

    merged: list[dict] = []
    used = set()
    for sw in side_work:
        key = norm(sw.get("company"))
        hit = None
        for i, ow in enumerate(ocr_work):
            if i in used:
                continue
            if key and (key in norm(ow.get("company")) or norm(ow.get("company")) in key):
                hit, idx = ow, i
                break
        if hit is not None:
            used.add(idx)
            merged.append({**sw,
                           "title": sw.get("title") or hit.get("title", ""),
                           "descriptions": hit.get("descriptions") or []})
        else:
            merged.append(dict(sw))
            notes.append(f"侧栏有但 OCR 缺：{sw.get('company')}")

    for i, ow in enumerate(ocr_work):
        if i not in used:
            merged.append(ow)
            notes.append(f"OCR 有但侧栏缺：{ow.get('company')}")

    edu = list((ocr_parts or {}).get("education") or [])
    if not edu and side.get("education"):
        edu = list(side["education"])

    return {"work": merged, "education": edu, "notes": notes}
