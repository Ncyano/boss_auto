"""macOS / Linux 下的窗口层实现（仅 CDP 后台模式）。

Windows 版依赖 user32/gdi32 做窗口定位与鼠标注入；macOS 没有这些，
所以这里把「窗口」抽象掉：整个浏览器 viewport 就是坐标系，
抓图与点击全部走 CDP 协议（Page.captureScreenshot / Input.dispatchMouseEvent）。

因此 macOS 上：
    · 必须用后台模式（capture="cdp"）；
    · 浏览器必须带 --remote-debugging-port 启动；
    · 页面不能被最小化（最小化后 Chromium 停止渲染）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import cdp as _cdp

# ---- 与 Windows 版同名同义的模块级状态 ----
_CAPTURE_MODE = ["cdp"]          # mac 只有 cdp
_NEED_REBASE = [False]
_WINDOW = [None]                 # (rect, title)


class CaptureDowngraded(RuntimeError):
    """抓图方式降级（mac 上无屏幕抓图可退，直接抛）。"""


# ---------------------------------------------------------------- 基础
def set_capture(mode: str) -> None:
    _CAPTURE_MODE[0] = "cdp"


def capture_mode() -> str:
    return "cdp"


def enable_dpi_awareness() -> None:
    """macOS 不需要 DPI 感知设置。"""
    return


def reset_cdp() -> None:
    try:
        _cdp.reset_cdp()
    except Exception:
        pass


def _cdp_client():
    return _cdp.shared()


# ---------------------------------------------------------------- 窗口
def find_window(title: str = "BOSS直聘"):
    """mac 上没有窗口枚举：把浏览器 viewport 当成窗口返回。

    返回 (handle, rect, title)；拿不到几何信息时给一个保守默认值。
    """
    try:
        g = _cdp.shared().geometry()
        rect = (0, 0, int(g.get("iw") or 1280), int(g.get("ih") or 800))
    except Exception:
        rect = (0, 0, 1280, 800)
    _WINDOW[0] = (rect, title or "BOSS直聘（CDP）")
    return (1, rect, _WINDOW[0][1])


def restore_window(rect=None) -> None:
    return


def grab_window(rect=None):
    """整页截图（CDP）。"""
    try:
        return _cdp.shared().screenshot()
    except Exception as exc:
        _NEED_REBASE[0] = True
        raise CaptureDowngraded(f"CDP 截图失败：{exc}") from exc


def _grab_screen(rect=None):
    raise CaptureDowngraded("macOS 不支持屏幕抓图（请使用后台模式）")


# ---------------------------------------------------------------- 输入
def click(x: int, y: int, *, tolerance: int = 40, double: bool = False,
          container: str | None = None, log=print) -> None:
    """按坐标点击（CSS 像素坐标）。"""
    _cdp.shared().click_at(int(x), int(y), double=double)


def click_css(x: int, y: int, log=print) -> None:
    click(x, y, log=log)


def move(x: int, y: int) -> None:
    """CDP 下无需预移动鼠标。"""
    return


def scroll(dy: int, x: int | None = None, y: int | None = None, log=print) -> None:
    _cdp.shared().wheel(int(dy), x, y)


def scroll_at(x: int, y: int, dy: int, log=print) -> None:
    _cdp.shared().wheel(int(dy), x, y)


def scroll_modal(dy: int, log=print) -> None:
    _cdp.shared().scroll_modal(int(dy))


def scroll_modal_top(log=print) -> None:
    _cdp.shared().scroll_modal_top()


def scroll_modal_bottom(log=print) -> None:
    _cdp.shared().scroll_modal_bottom()


def raw_wheel(dy: int, log=print) -> None:
    _cdp.shared().wheel(int(dy))


def press_escape() -> bool:
    try:
        _cdp.shared().press_escape()
        return True
    except Exception:
        return False


def close_dialog_by_js() -> bool:
    try:
        return bool(_cdp.shared().close_dialog_by_js())
    except Exception:
        return False


# ---------------------------------------------------------------- 业务动作
def greet(should_stop=None) -> bool:
    """点「打招呼」并确认变成「继续沟通」。"""
    client = _cdp.shared()
    if should_stop and should_stop():
        return False
    ok = client.click_element("打招呼", container="resumeGreet")
    if not ok:
        return False
    for _ in range(10):
        time.sleep(0.3)
        if client.greet_button_text().strip() in ("继续沟通", "继续聊"):
            return True
    return False


def greet_button_text() -> str:
    try:
        return _cdp.shared().greet_button_text()
    except Exception:
        return ""


def click_list_refresh(log=print) -> bool:
    try:
        return bool(_cdp.shared().click_element("刷新"))
    except Exception:
        return False


def refresh_page(wait: float = 3.0, log=print) -> bool:
    try:
        _cdp.shared()._send("Page.reload", {"ignoreCache": False})
        time.sleep(wait)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 启动浏览器
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def launch_debug_browser(url: str = "https://www.zhipin.com/web/chat/recommend",
                         wait: float = 25) -> bool:
    """启动带调试端口的 Chrome/Edge（独立 profile）。"""
    exe = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    if not exe:
        print("[warn] 没找到 Chrome/Edge，请手动安装或手动启动带调试端口的浏览器")
        return False
    profile = os.path.expanduser("~/Library/Application Support/boss-cdp-profile")
    args = [exe,
            "--remote-debugging-port=9222",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            url]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[warn] 启动浏览器失败：{exc}")
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if _cdp.available():
            return True
        time.sleep(1.0)
    return _cdp.available()
