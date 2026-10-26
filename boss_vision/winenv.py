"""窗口层：定位 BOSS 浏览器窗口、按窗口抓图、移动鼠标。

为什么按窗口而不是按整屏：实测用户把浏览器窗口缩小后，整屏固定坐标全部失效。
按窗口取相对坐标后，窗口移动/缩放都不影响识别结果。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import numpy as np
from PIL import ImageGrab

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

DWMWA_EXTENDED_FRAME_BOUNDS = 9
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000  # 必须带：否则 0..65535 只映射到主屏，双屏下 x 会被压缩一半
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
INPUT_MOUSE = 0
# 所有绝对坐标事件统一带这两个标志
ABSOLUTE_FLAGS = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("mi", MOUSEINPUT)]


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def find_window(keyword: str = "BOSS直聘") -> tuple[int, tuple[int, int, int, int], str] | None:
    """按标题关键字找可见的顶层窗口，返回 (hwnd, (x, y, w, h), title)。"""
    hits: list[tuple[int, tuple[int, int, int, int], str]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if keyword.lower() in title.lower():
            rect = wt.RECT()
            got = False
            try:
                if dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                                                ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
                    got = True
            except Exception:
                got = False
            if not got:
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
            hits.append((hwnd, (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top), title))
        return True

    user32.EnumWindows(_cb, 0)
    hits.sort(key=lambda h: h[1][2] * h[1][3], reverse=True)
    return hits[0] if hits else None


def grab_window(rect: tuple[int, int, int, int]) -> np.ndarray:
    """抓窗口区域，返回 BGR。"""
    x, y, w, h = rect
    im = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return np.asarray(im.convert("RGB"))[:, :, ::-1].copy()


_LAST_SEND_OK = True


def last_error() -> int:
    return ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else ctypes.GetLastError()


def _send(inp: INPUT) -> int:
    """发一个输入事件，返回 SendInput 实际插入的事件数（0 表示被系统丢弃）。"""
    global _LAST_SEND_OK
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wt.UINT
    n = int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))
    _LAST_SEND_OK = _LAST_SEND_OK and n == 1
    return n


def _abs(x: int, y: int) -> tuple[int, int]:
    """屏幕坐标 -> SendInput 的 0..65535 归一化坐标。"""
    vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return int((x - vx) * 65535 / (vw - 1)), int((y - vy) * 65535 / (vh - 1))


def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def move_to(x: int, y: int, duration: float = 0.35) -> None:
    """沿二次贝塞尔曲线移动鼠标，模拟人手（非瞬移）。"""
    sx, sy = cursor_pos()
    cx = sx + (x - sx) * 0.5 + (y - sy) * 0.12  # 轻微弧线
    cy = sy + (y - sy) * 0.5 - (x - sx) * 0.12
    steps = max(8, int(duration * 90))
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        px = int(u * u * sx + 2 * u * t * cx + t * t * x)
        py = int(u * u * sy + 2 * u * t * cy + t * t * y)
        ax, ay = _abs(px, py)
        mi = MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | ABSOLUTE_FLAGS, 0, None)
        _send(INPUT(INPUT_MOUSE, mi))
        time.sleep(duration / steps * 0.6)


class InputBlocked(RuntimeError):
    """注入被拦：光标没有到达目标位置，此时绝不能按下按键。"""


# ---------------- CDP 模式：光标完全不动 ----------------
_CDP_CLIENT = [None]


_CDP_SCROLL_OK = [True]      # CDP 滚不动时置 False，退回真实滚轮


def disable_cdp_scroll() -> None:
    """页面用虚拟滚动（BOSS 推荐列表在 iframe 里）时 CDP 滚轮可能无效，
    调用一次就让滚动退回真实鼠标，点击仍然走 CDP。"""
    _CDP_SCROLL_OK[0] = False


def reset_cdp_scroll() -> None:
    _CDP_SCROLL_OK[0] = True


def _cdp_mode() -> bool:
    """config.input.mode == "cdp" 且调试端口在线时，走无鼠标路径。"""
    try:
        import settings
        cfg = settings.load()
        mode = str((cfg.get("input") or {}).get("mode") or "").lower()
    except Exception:
        return False
    if mode != "cdp":
        return False
    try:
        import cdp as _cdp
        return _cdp.available()
    except Exception:
        return False


def _cdp_client():
    if _CDP_CLIENT[0] is None:
        import cdp as _cdp
        _CDP_CLIENT[0] = _cdp.CDP()
    return _CDP_CLIENT[0]


def reset_cdp() -> None:
    """浏览器重启后旧连接会失效，重连一次。"""
    try:
        if _CDP_CLIENT[0] is not None:
            _CDP_CLIENT[0].close()
    except Exception:
        pass
    _CDP_CLIENT[0] = None

def click(x: int, y: int, settle: tuple[float, float] = (0.06, 0.14), tolerance: int = 6) -> None:
    if _cdp_mode():
        try:
            _cdp_client().click(x, y)
            return
        except Exception:
            reset_cdp()          # 连接失效就退回真实鼠标，别卡住流程
    """移动到 (x, y)，**确认光标真的到位**后才按下左键。

    为什么要确认：按下事件的坐标若不带 MOVE 标志会被忽略，一旦"移动被拦而按键没被拦"，
    就会在光标当前位置误点。宁可报错也不能瞎点。
    """
    import random

    move_to(x, y)
    time.sleep(0.08)
    got = cursor_pos()
    if abs(got[0] - x) > tolerance or abs(got[1] - y) > tolerance:
        raise InputBlocked(
            f"光标未到位：期望 ({x},{y})，实际 {got}；SendInput 是否全部成功={_LAST_SEND_OK}，"
            f"GetLastError={last_error()}。"
            " 常见原因：进程完整性级别低于目标窗口（UIPI）、或运行在被限制的子进程/沙箱里。已放弃本次点击。"
        )
    time.sleep(random.uniform(0.05, 0.12))
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        ax, ay = _abs(x, y)
        mi = MOUSEINPUT(ax, ay, 0, flag | MOUSEEVENTF_MOVE | ABSOLUTE_FLAGS, 0, None)
        _send(INPUT(INPUT_MOUSE, mi))
        time.sleep(random.uniform(*settle))


KEYEVENTF_KEYUP = 0x0002
VK_ESCAPE = 0x1B
VK_F5 = 0x74


def press_key(vk: int, hold: tuple[float, float] = (0.05, 0.12)) -> None:
    """按一次真实键（按下/抬起），不是 SendKeys。"""
    import random

    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(random.uniform(*hold))
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def refresh_page(hwnd: int, wait: float = 0.3) -> bool:
    """刷新当前页面（F5）。

    F5 是键盘事件，只发给前台窗口；GUI 抢了前台时就会刷错窗口。
    所以先强制置前并确认，拿不到前台就返回 False（调用方据此报错），不要假装刷新成功。
    """
    if not focus(hwnd):
        return False
    time.sleep(wait)
    press_key(VK_F5)
    return True


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow())


def focus(hwnd: int) -> bool:
    """把目标窗口切到前台。SetForegroundWindow 常被系统拒绝，这里用几种方式硬切并校验。

    返回 True 表示确认目标窗口已经是前台窗口。
    """
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if foreground_hwnd() == hwnd:
            return True

        # 1) 直接切
        user32.SetForegroundWindow(hwnd)
        if foreground_hwnd() == hwnd:
            return True

        # 2) AttachThreadInput 绕过前台限制（经典做法）
        fg = foreground_hwnd()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel32.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            user32.AttachThreadInput(tid_fg, tid_me, True)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
            finally:
                user32.AttachThreadInput(tid_fg, tid_me, False)
        if foreground_hwnd() == hwnd:
            return True

        # 3) 老 API 兜底
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
        return foreground_hwnd() == hwnd
    except Exception:
        return False



def integrity_name(pid: int | None = None) -> str:
    """读进程令牌的完整性级别：Untrusted/Low/Medium/High/System。

    注入被 UIPI 拦的根因通常是：本进程的级别 低于 目标窗口的级别。
    """
    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    advapi32 = ctypes.windll.advapi32
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi32.OpenProcessToken.restype = wt.BOOL
    advapi32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wt.DWORD, ctypes.POINTER(wt.DWORD)]
    advapi32.GetTokenInformation.restype = wt.BOOL

    if pid is None:
        proc = kernel32.GetCurrentProcess()
        need_close = False
    else:
        proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        need_close = True
    if not proc:
        return "未知(打不开进程)"
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(token)):
            return "未知(打不开令牌)"
        try:
            size = wt.DWORD(0)
            advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(max(size.value, 64))
            if not advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf, size, ctypes.byref(size)):
                return "未知(读不到级别)"
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid:
                return "未知(空SID)"
            count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
            rid = int.from_bytes(ctypes.string_at(sid + 8 + 4 * (count - 1), 4), "little")
            return {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
                    0x2100: "Medium+", 0x3000: "High", 0x4000: "System"}.get(rid, hex(rid))
        finally:
            kernel32.CloseHandle(token)
    finally:
        if need_close:
            kernel32.CloseHandle(proc)


IL_RANK = {"Untrusted": 0, "Low": 1, "Medium": 2, "Medium+": 2, "High": 3, "System": 4}


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def injection_permission(hwnd: int) -> tuple[bool, str]:
    """判断当前进程能否向目标窗口注入输入：返回 (可否, 说明文本)。"""
    mine = integrity_name()
    target = integrity_name(window_pid(hwnd))
    ok = IL_RANK.get(mine, -1) >= IL_RANK.get(target, 99)
    msg = f"本进程完整性={mine}，BOSS 窗口进程完整性={target}"
    if not ok:
        if target.startswith("未知"):
            msg += "  → 读不到目标进程令牌，通常说明浏览器权限更高；请以管理员身份运行本程序"
        else:
            msg += "  → 权限不足：请以管理员身份运行本程序（或让浏览器降为普通权限）"
    return ok, msg


def scroll_modal(clicks: int = -3) -> None:
    """滚简历弹窗（找最长的可滚动容器），完全不用坐标和鼠标。"""
    if _cdp_mode() and _CDP_SCROLL_OK[0]:
        try:
            moved = _cdp_client().scroll_modal(-clicks * 120)
            if moved:
                return
        except Exception:
            reset_cdp()
    # 兜底：真实滚轮（坐标取屏幕中心）
    try:
        import ctypes
        u = ctypes.windll.user32
        cx = u.GetSystemMetrics(0) // 2
        cy = u.GetSystemMetrics(1) // 2
        scroll(cx, cy, clicks)
    except Exception:
        pass

def scroll_at(x: int, y: int, clicks: int = -3) -> None:
    """在 (x, y) 处滚动"该位置的容器"（简历弹窗/列表都能用），不移动鼠标。"""
    if _cdp_mode() and _CDP_SCROLL_OK[0]:
        try:
            _cdp_client().scroll_at(x, y, -clicks * 120)
            return
        except Exception:
            reset_cdp()
    # 兜底：真实滚轮
    scroll(x, y, clicks)

def scroll(x: int, y: int, clicks: int = -3, tolerance: int = 6) -> None:
    if _cdp_mode() and _CDP_SCROLL_OK[0]:
        try:
            # 约定 clicks<0 = 向下滚；用 JS 直接滚（BOSS 列表在 iframe 里且是虚拟滚动）
            before = _cdp_client().scroll_top()
            got = _cdp_client().scroll_js(-clicks * 120)
            if got != before:
                return
            _cdp_client().wheel(x, y, -clicks * 120)   # 退一步试滚轮事件
            return
        except Exception:
            reset_cdp()
    """在 (x, y) 处滚轮。clicks 为负表示向下滚。同样要求光标先到位。"""
    move_to(x, y, duration=0.2)
    time.sleep(0.06)
    got = cursor_pos()
    if abs(got[0] - x) > tolerance or abs(got[1] - y) > tolerance:
        raise InputBlocked(f"滚动前光标未到位：期望 ({x},{y})，实际 {got}，已放弃滚动。")
    user32.mouse_event(0x0800, 0, 0, int(clicks * 120), 0)  # MOUSEEVENTF_WHEEL


VK_CONTROL, VK_A, VK_C = 0x11, 0x41, 0x43


def hotkey(mod_vk: int, key_vk: int) -> None:
    """真实按下一组组合键（如 Ctrl+A）。"""
    import random

    user32.keybd_event(mod_vk, 0, 0, 0)
    time.sleep(random.uniform(0.03, 0.07))
    user32.keybd_event(key_vk, 0, 0, 0)
    time.sleep(random.uniform(0.04, 0.09))
    user32.keybd_event(key_vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(random.uniform(0.02, 0.05))
    user32.keybd_event(mod_vk, 0, KEYEVENTF_KEYUP, 0)


def clipboard_text(retries: int = 8) -> str:
    """读系统剪贴板文本。优先用 pywin32；否则退回 ctypes（注意 64 位句柄要声明 c_void_p）。"""
    import time as _t

    try:
        import win32clipboard as cb

        for _ in range(retries):
            try:
                cb.OpenClipboard()
                try:
                    return str(cb.GetClipboardData(cb.CF_UNICODETEXT) or "")
                finally:
                    cb.CloseClipboard()
            except Exception:
                _t.sleep(0.08)
        return ""
    except ImportError:
        pass

    CF_UNICODETEXT = 13
    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    ctypes.windll.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.GlobalLock.restype = ctypes.c_void_p
    ctypes.windll.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    for _ in range(retries):
        if user32.OpenClipboard(None):
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                ptr = ctypes.windll.kernel32.GlobalLock(h)
                try:
                    return ctypes.wstring_at(ptr) if ptr else ""
                finally:
                    ctypes.windll.kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        _t.sleep(0.08)
    return ""
