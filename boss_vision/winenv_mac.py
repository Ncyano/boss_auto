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


class InputBlocked(RuntimeError):
    """输入被阻断。mac 上走 CDP，不会发生；保留同名异常以兼容调用方。"""


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
def click(x: int, y: int, settle: float = 0.0, tolerance: int = 40) -> None:
    """按坐标点击（CSS 像素坐标）。签名与 Windows 版一致。"""
    _cdp.shared().click_at(int(x), int(y))
    if settle:
        time.sleep(float(settle))


def click_css(x: int, y: int, log=print) -> None:
    click(x, y, log=log)


def move(x: int, y: int) -> None:
    """CDP 下无需预移动鼠标。"""
    return


def scroll(x: int, y: int, clicks: int, tolerance: int = 6) -> None:
    """与 Windows 版同签名：在 (x, y) 处滚动 clicks 格（正=向下）。"""
    _cdp.shared().wheel(int(clicks) * 100, x, y)


def scroll_at(x: int, y: int, clicks: int) -> None:
    scroll(x, y, clicks)


def scroll_modal(clicks: int, x: int | None = None, y: int | None = None) -> None:
    _cdp.shared().scroll_modal(int(clicks) * 100)


def scroll_modal_top(clicks: int = 1, tolerance: int = 6) -> None:
    _cdp.shared().scroll_modal_top()


def scroll_modal_bottom(clicks: int = 100) -> None:
    _cdp.shared().scroll_modal_bottom()


def raw_wheel(x: int, y: int, clicks: int) -> None:
    _cdp.shared().wheel(int(clicks) * 100, x, y)


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


# ---------------------------------------------------------------- 权限与焦点
def injection_permission(hwnd=None) -> tuple[bool, str]:
    """mac 上不做 Win32 注入，直接返回通过（保持与 Windows 版同返回结构）。"""
    return True, "CDP 注入（macOS 无需 UIPI 检查）"


def focus(hwnd=None) -> bool:
    """CDP 下无需前台窗口。"""
    return True


def foreground_hwnd():
    return 1


def integrity_name(pid: int | None = None) -> str:
    return "n/a"


def window_pid(hwnd=None) -> int:
    return 0


def press_key(vk: int, hold: float = 0.05) -> None:
    """按虚拟键码（Windows VK）。mac 上只处理 Esc / Enter。"""
    if vk in (0x1B,):
        press_escape()
    return


def cursor_pos() -> tuple[int, int]:
    return (0, 0)


def move_to(x: int, y: int, duration: float = 0.0) -> None:
    """CDP 下不移动真实鼠标。"""
    return


def hotkey(mods, key) -> None:
    return


def clipboard_text(hwnd=None) -> str:
    return ""


def disable_cdp_scroll() -> None:
    return


def reset_cdp_scroll() -> None:
    return


def last_error() -> str:
    return ""


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


def modal_open_dom() -> bool:
    """简历弹窗是否打开（DOM 判定）。"""
    try:
        return bool(_cdp.shared().modal_open_dom())
    except Exception:
        return False


def click_list_refresh() -> bool:
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
                         wait: float = 25, port: int = 9222) -> bool:
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
