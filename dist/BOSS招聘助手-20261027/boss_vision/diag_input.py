"""输入注入诊断：为什么鼠标不动？

在你自己的普通终端里跑（不要用受限终端/沙箱）：

    python diag_input.py

它会逐项试 5 种注入方式并打印系统返回值，用来区分「代码问题」「权限问题」「环境问题」。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import winenv

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def integrity_of(pid: int | None = None) -> str:
    """读进程令牌的完整性级别：Low/Medium/High/System。"""
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
    advapi32.GetTokenInformation.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)
    ]
    advapi32.GetTokenInformation.restype = wt.BOOL

    if pid is None:
        proc = kernel32.GetCurrentProcess()
        need_close = False
    else:
        proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        need_close = True
    if not proc:
        return f"打不开进程 {pid}"

    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(token)):
            return "OpenProcessToken 失败"
        try:
            size = wt.DWORD(0)
            advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(max(size.value, 64))
            if not advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf, size, ctypes.byref(size)):
                return "GetTokenInformation 失败"
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid:
                return "SID 为空"
            count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
            last = ctypes.string_at(sid + 8 + 4 * (count - 1), 4)
            rid = int.from_bytes(last, "little")
            names = {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium+",
                     0x3000: "High", 0x4000: "System"}
            return names.get(rid, hex(rid))
        finally:
            kernel32.CloseHandle(token)
    finally:
        if need_close:
            kernel32.CloseHandle(proc)


def main() -> int:
    winenv.enable_dpi_awareness()
    print("=" * 62)
    print("输入注入诊断")
    print("=" * 62)

    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    print(f"虚拟桌面: 原点({vx},{vy}) 尺寸 {vw}x{vh}")
    print(f"主屏尺寸: {pw}x{ph}")
    print(f"当前进程完整性: {integrity_of()}   (需要 Medium 或更高)")

    win = winenv.find_window("BOSS直聘")
    if win:
        hwnd, rect, _t = win
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        print(f"BOSS 窗口进程 pid={pid.value} 完整性: {integrity_of(pid.value)}")
        print(f"BOSS 窗口位置: {rect}")

    if "--check" in sys.argv:
        me = integrity_of()
        print()
        if me in {"Medium", "Medium+", "High", "System"}:
            print("=> 权限够，可以跑 driver.py run --live")
        else:
            print("=> 权限不够，换一个从开始菜单启动的终端，或以管理员身份运行")
        return 0

    in_scope = input("\n即将移动你的鼠标做 5 项测试（约 3 秒），继续？[y/N] ").strip().lower()
    if in_scope != "y":
        print("已取消")
        return 0

    sx, sy = winenv.cursor_pos()
    print(f"\n起始光标: ({sx},{sy})")

    target = (vw // 2, vh // 2)  # 虚拟桌面中点，避免落在任何按钮上
    results = []

    # 1) SendInput + ABSOLUTE + VIRTUALDESK（我们现在的做法）
    ax, ay = winenv._abs(*target)
    mi = winenv.MOUSEINPUT(ax, ay, 0, winenv.MOUSEEVENTF_MOVE | winenv.ABSOLUTE_FLAGS, 0, None)
    n = winenv._send(winenv.INPUT(winenv.INPUT_MOUSE, mi))
    time.sleep(0.15)
    got = winenv.cursor_pos()
    results.append(("SendInput(ABS|VIRTUALDESK)", n, got, got == target))

    # 2) SendInput 不带 VIRTUALDESK（旧行为，双屏会压缩坐标）
    mi = winenv.MOUSEINPUT(ax, ay, 0, winenv.MOUSEEVENTF_MOVE | winenv.MOUSEEVENTF_ABSOLUTE, 0, None)
    n = winenv._send(winenv.INPUT(winenv.INPUT_MOUSE, mi))
    time.sleep(0.15)
    results.append(("SendInput(ABS, 无VIRTUALDESK)", n, winenv.cursor_pos(), None))

    # 3) 老 API mouse_event（不经 SendInput 队列）
    tx, ty = target
    norm_x, norm_y = winenv._abs(tx, ty)
    user32.mouse_event(winenv.MOUSEEVENTF_MOVE | winenv.ABSOLUTE_FLAGS, norm_x, norm_y, 0, 0)
    time.sleep(0.15)
    results.append(("mouse_event(ABS|VIRTUALDESK)", "n/a", winenv.cursor_pos(), None))

    # 4) SetCursorPos（不经输入队列，通常总能生效）
    ok = bool(user32.SetCursorPos(tx, ty))
    time.sleep(0.15)
    results.append(("SetCursorPos", ok, winenv.cursor_pos(), None))

    # 5) SetCursorPos 到偏离点，证明它能动
    ok = bool(user32.SetCursorPos(tx + 40, ty + 40))
    time.sleep(0.1)
    results.append(("SetCursorPos(+40)", ok, winenv.cursor_pos(), None))

    print("\n{:<32} {:<12} {:<16} {}".format("方式", "返回值", "之后的坐标", "是否精确到位"))
    print("-" * 78)
    for name, ret, got, exact in results:
        print("{:<32} {:<12} {:<16} {}".format(name, str(ret), str(got), "" if exact is None else exact))

    # 收尾：回到原位
    user32.SetCursorPos(sx, sy)
    print(f"\n已把光标放回 ({sx},{sy})")
    print("\n判读：")
    print("  · 1 的返回值是 1 且坐标精确到位 -> 注入正常，可以用 --live")
    print("  · 1 的返回值是 0 -> 系统直接丢弃事件，典型是 UIPI/权限或受限环境")
    print("  · 1 返回 1 但坐标不对 -> 多屏坐标换算问题（看 2 与 1 的差异）")
    print("  · 只有 4/5 能生效 -> SendInput 被拦但 SetCursorPos 可用，可改走 SetCursorPos+鼠标事件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
