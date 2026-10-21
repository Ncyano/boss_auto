"""自检：一次跑完所有前置条件，逐项 PASS/FAIL。

在你自己的普通终端里运行（不要在受限沙箱里跑，沙箱会拦鼠标键盘注入）：

    python selftest.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import vision
import winenv

OK, BAD = "PASS", "FAIL"
rows: list[tuple[str, str, str]] = []


def add(name: str, ok: bool, detail: str = "") -> bool:
    rows.append((name, OK if ok else BAD, detail))
    return ok


def main() -> int:
    winenv.enable_dpi_awareness()

    dpi = ctypes.windll.user32.GetDpiForSystem()
    add("系统 DPI", True, f"{dpi}（96 = 100%，坐标无需缩放）")

    found = winenv.find_window("BOSS直聘")
    add("找到 BOSS 窗口", found is not None,
        f"{found[2][:40]} rect={found[1]}" if found else "请先打开 BOSS 直聘招聘端页面")
    if not found:
        return report()
    hwnd, rect, _t = found

    img = winenv.grab_window(rect)
    add("窗口截图", img.size > 0, f"{img.shape[1]}x{img.shape[0]}")

    boxes = vision.detect_teal(img)
    btns, wides = vision.list_buttons(boxes), vision.wide_buttons(boxes)
    state = vision.guess_state(btns, wides)
    add("页面状态识别", state != "unknown", f"state={state} 列表按钮={len(btns)} 弹窗按钮={len(wides)}")

    # 输入注入：移动光标再读回，位置不变说明注入被拦
    before = winenv.cursor_pos()
    winenv.move_to(before[0] + 60, before[1] + 40, duration=0.2)
    time.sleep(0.15)
    after = winenv.cursor_pos()
    moved = (after != before)
    add("鼠标注入 (SendInput)", moved, f"{before} -> {after}")

    if moved:
        winenv.move_to(before[0], before[1], duration=0.2)

    clip = winenv.clipboard_text()
    add("剪贴板可读", True, f"当前 {len(clip)} 字（读简历用）")

    print()
    for name, status, detail in rows:
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    print()
    if not moved:
        print("鼠标注入失败：常见原因是进程权限低于目标窗口（UIPI），或运行在受限沙箱里。")
        print("请用普通（非提权、非沙箱）终端运行本脚本，并确保 BOSS 窗口没有被最小化。")
    return 0 if all(r[1] == OK for r in rows) else 1


def report() -> int:
    for name, status, detail in rows:
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return 1


if __name__ == "__main__":
    sys.exit(main())
