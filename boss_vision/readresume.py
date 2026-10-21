"""从已打开的简历弹窗里取文本：点正文 -> Ctrl+A -> Ctrl+C -> 读剪贴板。

比 OCR 更准（等于人手动复制），代价是占用剪贴板。只在 --live 下真的操作。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import winenv

SECTION_START = ("工作经历", "教育经历")
SECTION_END = ("项目经历", "资格证书", "求职意向", "自我评价", "附件", "举报", "不合适")


def slice_sections(text: str) -> dict[str, str]:
    """把人肉复制得到的整页文本切成「工作经历 / 教育经历」两段。"""
    out: dict[str, str] = {}
    for sec in SECTION_START:
        i = text.find(sec)
        if i < 0:
            continue
        j = len(text)
        for end in SECTION_END:
            k = text.find(end, i + len(sec))
            if k > i:
                j = min(j, k)
        out[sec] = re.sub(r"\n{2,}", "\n", text[i:j]).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--anchor", default="1250,600", help="简历正文里一个安全点的窗口内坐标")
    a = ap.parse_args()

    winenv.enable_dpi_awareness()
    found = winenv.find_window("BOSS直聘")
    if not found:
        print("没找到 BOSS 窗口")
        return 2
    hwnd, rect, _title = found
    ox, oy = rect[0], rect[1]
    ax, ay = (int(v) for v in a.anchor.split(","))
    print(f"窗口 ({ox},{oy}) {rect[2]}x{rect[3]}；锚点 窗口内({ax},{ay}) 屏幕({ox + ax},{oy + ay})")

    if not a.live:
        print("dry-run：不点不复制。加 --live 执行。")
        return 0

    winenv.focus(hwnd)
    time.sleep(0.4)
    winenv.click(ox + ax, oy + ay)          # 让焦点落在正文里
    time.sleep(0.3)
    winenv.hotkey(winenv.VK_CONTROL, winenv.VK_A)
    time.sleep(0.3)
    winenv.hotkey(winenv.VK_CONTROL, winenv.VK_C)
    time.sleep(0.6)
    text = winenv.clipboard_text()
    print(f"剪贴板长度: {len(text)}")
    secs = slice_sections(text)
    for k, v in secs.items():
        print(f"\n=== {k} ({len(v)} 字) ===")
        print(v[:600])
    if not secs:
        print("\n没切出小节，前 600 字：")
        print(text[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
