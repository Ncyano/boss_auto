"""BOSS 招聘端自动开简历 —— 带界面的控制台。

    .venv\\Scripts\\pythonw.exe boss_vision\\gui.py

窗口里可以设「有效人次目标」，点开始就跑，点停止随时中断（连 15~25 秒的等待也会被打断）。
日志实时显示在窗口里，同时自动落盘到 boss_vision\\runs\\<时间>-console.log。
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import driver  # noqa: E402


class _QueueWriter:
    """把 driver 的 print 输出导进队列，交给主线程刷到界面上。"""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> None:
        if s:
            self.q.put(s)

    def flush(self) -> None:
        pass


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化成 1:23 / 1:02:03 这种。"""
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.worker: threading.Thread | None = None
        self.boot_ts = time.time()          # 窗口启动时间，用来提示"代码已更新"
        self.log_queue: queue.Queue = queue.Queue()
        self.start_ts: float | None = None
        self.last_elapsed: float = 0.0            # 上一次任务的耗时（秒）

        root.title("BOSS 自动开简历 · 控制台")
        root.geometry("980x720")
        root.minsize(820, 560)

        # ---------- 参数区 ----------
        top = ttk.LabelFrame(root, text="参数", padding=10)
        top.pack(fill="x", padx=10, pady=(10, 6))

        self.var_limit = tk.IntVar(value=20)
        self.var_url = tk.StringVar(value="")
        self.var_background = tk.BooleanVar(value=False)
        # 滚动屏数 / 刷新次数不再暴露在界面，用固定默认值（12 屏、3 次）
        self.var_scroll = tk.IntVar(value=12)
        self.var_refresh = tk.IntVar(value=3)
        self.var_gap_min = tk.DoubleVar(value=3.0)
        self.var_gap_max = tk.DoubleVar(value=5.0)
        self.var_icon = tk.BooleanVar(value=True)
        self.var_dry = tk.BooleanVar(value=False)
        self.var_refresh_page = tk.BooleanVar(value=False)   # 默认：不刷新，只往下滚
        self.var_text_scan = tk.BooleanVar(value=False)      # 纯文字找推荐块（慢，配色变了才用）

        def add_row(r: int, label: str, widget, hint: str = "") -> None:
            if label:
                ttk.Label(top, text=label).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=3)
            widget.grid(row=r, column=1, sticky="w", pady=3)
            if hint:
                ttk.Label(top, text=hint).grid(row=r, column=2, sticky="w", padx=8)

        # 识别方式在开跑前就显示出来：按 config 里的 pipeline 显示真实链路
        try:
            import vlm as _vlm
            _cfg = _vlm.load_config()
            _pipeline = str(_cfg.get("pipeline") or "vlm").lower()
            if not _vlm.api_key(_cfg):
                _reader_text = "无视觉模型 key（会退回本地 OCR，较慢且易错）"
            elif _pipeline == "ocr":
                _ocr = _cfg.get("ocr_model") or _vlm.OCR_DEFAULT_MODEL
                _struct = ""
                try:
                    import judge as _j
                    _struct = _j.load_config().get("model", "")
                except Exception:
                    pass
                _reader_text = (f"OCR 链路  {_ocr}" + (f" → {_struct} 结构化" if _struct else "")
                                + "（约 10 秒/屏，比视觉模型快近一倍）")
            else:
                _reader_text = f"视觉模型  {_cfg.get('model')}（较慢，约 16~20 秒/屏）"
        except Exception as exc:
            _reader_text = f"识别方式读取失败：{exc}"
        status = ttk.Label(top, text=f"识别方式：{_reader_text}", foreground="#0a6")
        status.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        add_row(1, "打招呼目标", ttk.Spinbox(top, from_=1, to=500, textvariable=self.var_limit, width=8),
                "（只有打招呼成功的才算一次）")

        add_row(7, "网址", ttk.Entry(top, textvariable=self.var_url, width=54),
                "（可选，填了就先打开这个页面）")
        add_row(8, "后台", ttk.Checkbutton(
            top, text="后台模式（浏览器窗口可被遮挡；截图改用浏览器自渲染）",
            variable=self.var_background))

        gap_row = ttk.Frame(top)
        ttk.Label(gap_row, text="间隔秒数").pack(side="left")
        ttk.Spinbox(gap_row, from_=0, to=600, increment=1, textvariable=self.var_gap_min, width=6).pack(side="left", padx=4)
        ttk.Label(gap_row, text="~").pack(side="left")
        ttk.Spinbox(gap_row, from_=0, to=600, increment=1, textvariable=self.var_gap_max, width=6).pack(side="left", padx=4)
        ttk.Label(gap_row, text="（每人间随机）").pack(side="left")
        add_row(2, "", gap_row)

        add_row(3, "判据", ttk.Checkbutton(top, text="检测「同事沟通进度」人像图标（勾掉则只按文字淡化）",
                                           variable=self.var_icon))
        add_row(4, "刷新", ttk.Checkbutton(
            top, text="滚动用尽时刷新页面（默认关闭：只向下滚，不刷新）", variable=self.var_refresh_page))
        add_row(5, "块识别", ttk.Checkbutton(
            top, text="用文字找推荐块（不依赖配色，较慢）", variable=self.var_text_scan))
        add_row(6, "模式", ttk.Checkbutton(top, text="试运行（只识别不点击）", variable=self.var_dry))

        # ---------- 按钮 ----------
        btns = ttk.Frame(root, padding=(10, 0))
        btns.pack(fill="x", pady=6)
        self.btn_start = ttk.Button(btns, text="▶  开始", command=self.on_start, width=14)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="■  停止", command=self.on_stop, width=14, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(btns, text="状态：空闲")
        self.lbl_status.pack(side="left", padx=16)
        self.lbl_elapsed = ttk.Label(btns, text="耗时 --:--", foreground="#06c")
        self.lbl_elapsed.pack(side="left", padx=8)

        # ---------- 进度条 ----------
        prog = ttk.Frame(root, padding=(10, 0))
        prog.pack(fill="x")
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.bar.pack(fill="x")

        # ---------- 日志 ----------
        box = ttk.LabelFrame(root, text="运行日志", padding=6)
        box.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.text = tk.Text(box, wrap="none", height=24, font=("Consolas", 9))
        yscroll = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=yscroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.text.configure(state="disabled")

        self.root.after(120, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 日志与状态刷新 ----------
    def append(self, s: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", s)
        # 行数太多就截掉前面
        if int(self.text.index("end-1c").split(".")[0]) > 3000:
            self.text.delete("1.0", "500.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def pump(self) -> None:
        while True:
            try:
                chunk = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append(chunk)

        p = driver.PROGRESS
        running = p.get("state") == "running"
        if running:
            target = max(1, p.get("target") or 1)
        else:
            try:
                target = max(1, int(self.var_limit.get()))
            except Exception:
                target = max(1, p.get("target") or 1)
        done = p.get("valid", 0)
        self.bar["value"] = min(100, done * 100 / target)
        state_map = {"idle": "空闲", "running": "运行中", "done": "已完成", "stopped": "已停止"}
        st = state_map.get(p.get("state", "idle"), p.get("state"))
        self.lbl_status.configure(
            text=f"状态：{st}   已打招呼 {done}/{target}   尝试 {p.get('handled', 0)} 次")

        # 任务耗时：运行中实时走，结束后显示总时长
        if self.start_ts:
            elapsed = time.time() - self.start_ts
            self.last_elapsed = elapsed
            running = p.get("state") == "running"
            self.lbl_elapsed.configure(
                text=("耗时 " if running else "总耗时 ") + _fmt_duration(elapsed),
                foreground="#06c" if running else "#070")
        else:
            self.lbl_elapsed.configure(text="耗时 --:--", foreground="#888")

        if self.worker and self.worker.is_alive():
            self.root.after(300, self.pump)
        else:
            if self.worker is not None:
                self.worker = None
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
            self.root.after(500, self.pump)

    # ---------- 动作 ----------
    def on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        # 后台模式下不依赖窗口标题（浏览器可最小化/被遮挡），跳过窗口检查
        if not bool(self.var_background.get()) and vision_window_missing():
            if not messagebox.askyesno("提示", "没检测到标题含「BOSS直聘」的窗口。\n仍然开始吗？"):
                return

        # 代码版本提示：GUI 一旦启动就固定用内存里的模块，改过的文件不重启不会生效
        drv_mtime = (_HERE / "driver.py").stat().st_mtime
        fmt = "%Y-%m-%d %H:%M:%S"
        print(f"程序目录: {_HERE}")
        print(f"解释器: {sys.executable}")
        print(f"代码版本: driver.py 修改于 {time.strftime(fmt, time.localtime(drv_mtime))}"
              f" | 本窗口启动于 {time.strftime(fmt, time.localtime(self.boot_ts))}")
        if drv_mtime > self.boot_ts:
            print("⚠️ 检测到 driver.py 在你启动窗口之后被修改过："
                  "本次仍使用内存中的旧代码，请关闭窗口重新启动！")

        root_dir = _HERE / "runs"
        root_dir.mkdir(exist_ok=True)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

        driver.reset_control()

        # 开跑前先体检权限：UIPI 拦截时点了也不动，提前说清楚
        ok, msg = check_injection_permission()
        print(("✓ " if ok else "✗ ") + msg)
        if not ok and not messagebox.askyesno(
                "权限可能不足",
                msg + "\n\n建议：关闭本窗口，右键 run-gui.cmd → 以管理员身份运行。\n仍要继续吗？"):
            return
        self.start_ts = time.time()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        kwargs = dict(
            live=not self.var_dry.get(),
            limit=int(self.var_limit.get()),
            max_scrolls=None,    # 不限制滚动次数（连续无进展会自动停）
            max_refresh=None,    # 不限制刷新次数
            read="scroll",
            anchor=(1250, 600),
            min_gap=float(self.var_gap_min.get()),
            max_gap=float(self.var_gap_max.get()),
            icon_check=bool(self.var_icon.get()),
            count_valid=True,          # 按"有效人次"计数
            allow_refresh=bool(self.var_refresh_page.get()),
            block_text_scan=bool(self.var_text_scan.get()),
            capture="cdp" if bool(self.var_background.get()) else "screen",
            url=(self.var_url.get() or "").strip(),
        )

        self.worker = threading.Thread(target=self._run_worker, kwargs=kwargs, daemon=True)
        self.worker.start()

    def _run_worker(self, **kwargs) -> None:
        old_out, old_err = sys.stdout, sys.stderr
        w = _QueueWriter(self.log_queue)
        sys.stdout = w
        sys.stderr = w
        try:
            rc = driver.run(**kwargs)
            print(f"\n[退出码 {rc}]")
        except Exception as exc:  # 任何异常都显示出来，不要静默
            import traceback

            print(f"\n[异常] {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stdout)
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def on_stop(self) -> None:
        driver.request_stop()
        self.lbl_status.configure(text="状态：正在停止…（当前动作结束后退出）")
        self.btn_stop.configure(state="disabled")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("确认", "任务还在运行，确定退出吗？\n（会立刻停止点击）"):
                return
            driver.request_stop()
        self.root.destroy()


def check_injection_permission() -> tuple[bool, str]:
    """检查本进程能否向 BOSS 窗口注入输入。"""
    try:
        import winenv

        winenv.enable_dpi_awareness()
        found = winenv.find_window("BOSS直聘")
        if not found:
            return True, "没找到 BOSS 窗口，跳过权限检查"
        return winenv.injection_permission(found[0])
    except Exception as exc:
        return True, f"权限检查失败（忽略）：{exc}"


def vision_window_missing() -> bool:
    try:
        import winenv

        winenv.enable_dpi_awareness()
        return winenv.find_window("BOSS直聘") is None
    except Exception:
        return False


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


def _install_crash_log() -> None:
    """把未捕获异常 / 进程崩溃写到 runs/crash.log，方便现场排查闪退。"""
    import faulthandler
    from datetime import datetime
    try:
        runs = _HERE / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        fh = open(runs / "crash.log", "a", encoding="utf-8", buffering=1)
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
        faulthandler.enable(file=fh, all_threads=True)

        def _hook(exc_type, exc, tb):
            import traceback
            fh.write(f"\n[未捕获异常] {exc_type.__name__}: {exc}\n")
            traceback.print_exception(exc_type, exc, tb, file=fh)
            fh.flush()
            traceback.print_exception(exc_type, exc, tb, file=sys.__stderr__)
        sys.excepthook = _hook
    except Exception:
        pass


if __name__ == "__main__":
    _install_crash_log()
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        from datetime import datetime
        try:
            runs = _HERE / "runs"
            runs.mkdir(parents=True, exist_ok=True)
            with open(runs / "crash.log", "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 崩溃 =====\n")
                fh.write(f"{type(exc).__name__}: {exc}\n")
                traceback.print_exc(file=fh)
        except Exception:
            pass
        traceback.print_exc()
        input("\n按回车退出…")
        raise
