"""POC：直接从 DOM 读简历文本（不走 OCR / 视觉模型）。

用法：
    python tools/poc_dom.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))

import cdp  # noqa: E402

FIND = (
    "(() => { const D=[document];"
    " for (const f of document.querySelectorAll('iframe')) {"
    "   try { if (f.contentDocument) D.push(f.contentDocument); } catch(e) {} }"
    " for (const d of D) { const el=d.querySelector('.resume-detail-wrap');"
    "   if (el) return el; } return null; })()"
)

READ = (
    "(() => { const D=[document];"
    " for (const f of document.querySelectorAll('iframe')) {"
    "   try { if (f.contentDocument) D.push(f.contentDocument); } catch(e) {} }"
    " for (const d of D) { const el=d.querySelector('.resume-detail-wrap');"
    "   if (!el) continue;"
    "   const t=(el.innerText||'').replace(/\\r/g,'');"
    "   return {text: t, len: t.length, h: el.scrollHeight, ch: el.clientHeight};"
    " } return null; })()"
)


def main() -> int:
    if not cdp.available():
        print("端口不可用")
        return 1
    cli = cdp.shared()

    t0 = time.time()
    r = cli._call("Runtime.evaluate", {"expression": READ, "returnByValue": True})
    dt = time.time() - t0
    data = (r.get("result") or {}).get("value")
    if not data:
        print("没有找到 .resume-detail-wrap，或容器内无文本")
        return 2

    text = data["text"]
    print(f"  耗时 {dt*1000:.0f} ms   字数 {data['len']}   容器 {data['h']}px/视口 {data['ch']}px")
    print(f"  可滚 {data['h']-data['ch']}px（>0 说明还有未渲染内容）")
    print("\n--- 前 25 行 ---")
    for i, line in enumerate(text.splitlines()[:25], 1):
        print(f"  {i:>2}. {line.strip()[:70]}")

    # 姓名：前 5 行里第一个 2~4 汉字
    name = ""
    for line in text.splitlines()[:5]:
        t = line.strip()
        if re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", t):
            name = t
            break

    # 公司：粗判——以「有限公司/科技/集团/工作室/中心」等结尾的行
    comp = [l.strip() for l in text.splitlines()
            if re.search(r"(有限公司|股份|集团|科技|工作室|事务所|中心|商行|门店)$", l.strip())]
    # 时间段
    periods = re.findall(r"\d{4}[.\-/]\d{1,2}\s*[-–—]\s*(?:\d{4}[.\-/]\d{1,2}|至今)", text)

    print("\n--- 结构化提取 ---")
    print(f"  姓名: {name or '(未识别)'}")
    print(f"  公司候选 {len(comp)} 个: {comp[:6]}")
    print(f"  时间段 {len(periods)} 个: {periods[:6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
