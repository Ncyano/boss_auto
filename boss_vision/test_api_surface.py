"""方法完整性自检：不连浏览器，确认 CDP / winenv 里该有的方法都在。

用来防止"清理重复定义时把被依赖的方法删掉"这类回归。

    .venv\\Scripts\\python.exe boss_vision\\test_api_surface.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cdp  # noqa: E402
import winenv  # noqa: E402

CDP_REQUIRED = [
    # 连接与基础（available 是模块级函数，单列）
    "geometry", "set_window_rect", "screenshot", "navigate",
    # 点击
    "click", "click_css", "click_text", "click_element", "move",
    # 弹窗滚动
    "_modal_js", "scroll_modal", "modal_scroll_top",
    "scroll_modal_top", "scroll_modal_bottom",
    # 列表滚动
    "scroll_js", "scroll_top", "scroll_at", "scroll_css", "wheel",
    # 关闭与按键
    "press_escape", "close_dialog_by_js",
    # 打招呼相关
    "greet_button_text", "find_text",
]

WINENV_REQUIRED = [
    "grab_window", "set_capture", "capture_mode",
    "click", "scroll", "scroll_at", "scroll_modal", "scroll_modal_top",
    "greet", "press_escape", "close_dialog_by_js",
    "restore_window", "launch_debug_browser", "raw_wheel",
]

# 这些方法必须"只读"，不能偷偷改变滚动位置
READONLY = {"modal_scroll_top": '"get"'}
MUTATING = {"scroll_modal_top": '"top"'}


def check(cls, names, label) -> list[str]:
    missing = [n for n in names if not hasattr(cls, n)]
    if missing:
        print(f"  [FAIL] {label} 缺少方法: {missing}")
    else:
        print(f"  [PASS] {label} 方法齐全（{len(names)} 个）")
    return missing


def main() -> int:
    bad = 0
    bad += len(check(cdp.CDP, CDP_REQUIRED, "CDP"))
    bad += len(check(winenv, [w for w in WINENV_REQUIRED if hasattr(winenv, w) or True], "winenv"))

    for name, needle in READONLY.items():
        src = inspect.getsource(getattr(cdp.CDP, name))
        ok = needle in src
        print(f"  {'[PASS]' if ok else '[FAIL]'} {name} 应只读（含 {needle}）")
        bad += 0 if ok else 1

    for name, needle in MUTATING.items():
        src = inspect.getsource(getattr(cdp.CDP, name))
        ok = needle in src
        print(f"  {'[PASS]' if ok else '[FAIL]'} {name} 应执行回顶（含 {needle}）")
        bad += 0 if ok else 1

    # 关键：滚动位置读取不能顺手回顶
    src = inspect.getsource(cdp.CDP.modal_scroll_top)
    if '_modal_js("top")' in src or "_modal_js('top')" in src:
        bad += 1
    elif False:
        print("  [FAIL] modal_scroll_top 里出现了回顶操作")
        bad += 1
    else:
        print("  [PASS] modal_scroll_top 不含回顶操作")

    print(f"\n{'全部通过' if bad == 0 else str(bad) + ' 项失败'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
