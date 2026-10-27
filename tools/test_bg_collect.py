"""后台模式采集稳定度测试：连续读 N 份在线简历，但不打招呼。

用法：
    .venv\\Scripts\\python.exe tools\\test_bg_collect.py --n 3

输出每人：耗时 / 屏数 / 字数 / 工作条数 / 教育条数 / 是否采集完整，
最后给出汇总（成功率、平均耗时）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import cdp  # noqa: E402
import driver  # noqa: E402
import resume_reader  # noqa: E402
import winenv  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="读几份简历")
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口没开：先 run-edge-debug.cmd")
        return 1
    winenv.set_capture("cdp")

    found = winenv.find_window("BOSS直聘")
    if not found:
        print("找不到 BOSS 窗口")
        return 1
    hwnd, rect, title = found
    print(f"  窗口: {title[:40]}")
    print(f"  抓图: {winenv.capture_mode()}\n")

    done = 0
    results = []
    tried = 0
    while done < args.n and tried < args.n * 3:
        tried += 1
        img = winenv.grab_window(rect)
        p = driver.plan(img, True)
        todo = p.get("todo") or []
        if p["state"] != "list" or not todo:
            print(f"  [{tried}] 状态={p['state']} 可点={len(todo)} -> 滚动")
            winenv.scroll(rect[2] // 2, rect[3] // 2, -3)
            time.sleep(1.0)
            continue

        idx = todo[0]
        card = p["cards"][idx]
        cx, cy = card.x + card.w // 2, card.y + card.h // 2
        t0 = time.time()
        winenv.click(cx, cy)
        time.sleep(1.0)

        opened = False
        p2 = None
        for _ in range(8):
            p2 = driver.plan(winenv.grab_window(rect), True)
            if p2["state"] == "resume_modal":
                opened = True
                break
            time.sleep(0.5)

        if not opened:
            print(f"  [{tried}] 点不开简历（y={card.y}）-> 跳过")
            try:
                winenv.press_escape()
                time.sleep(0.6)
            except Exception:
                pass
            continue

        name = ""
        try:
            name = resume_reader.card_name(winenv.grab_window(rect), p["cards"][idx])
        except Exception:
            pass

        try:
            rr = resume_reader.read_resume_fast(rect, p2["wides"][0], log=print,
                                                name_hint=name)
        except Exception as exc:
            print(f"  [{tried}] 读取异常: {exc}")
            rr = None

        dt = time.time() - t0
        if rr:
            ok = bool(rr.work) and bool(rr.education)
            results.append({"name": name, "sec": dt, "screens": rr.screen_count,
                            "chars": len(rr.text or ""), "work": len(rr.work or []),
                            "edu": len(rr.education or []), "ok": ok})
            print(f"  [{done + 1}/{args.n}] {name or '(无名)'}  {dt:.0f}s  "
                  f"{rr.screen_count} 屏  {len(rr.text or '')} 字  "
                  f"工作 {len(rr.work or [])} / 教育 {len(rr.education or [])}  "
                  f"{'✓' if ok else '✗ 不完整'}")
            done += 1

        try:
            driver.close_modal(rect, (rect[0], rect[1]), p2["wides"], icon_check=True)
        except Exception as exc:
            print(f"     [warn] 关弹窗失败：{exc}")
            winenv.press_escape()
        time.sleep(1.2)

    print("\n  === 汇总 ===")
    if not results:
        print("  没有成功读取的样本")
        return 1
    ok_n = sum(1 for r in results if r["ok"])
    avg = sum(r["sec"] for r in results) / len(results)
    print(f"  样本 {len(results)} 份，采集完整 {ok_n} 份（{ok_n * 100 // len(results)}%）")
    print(f"  平均耗时 {avg:.0f} 秒/人")
    print(f"  平均屏数 {sum(r['screens'] for r in results) / len(results):.1f}")
    print(f"  平均字数 {sum(r['chars'] for r in results) // len(results)}")
    for r in results:
        print(f"    {r['name'] or '(无名)':8} {r['sec']:5.0f}s  {r['screens']}屏 "
              f"{r['chars']:5}字  工作{r['work']} 教育{r['edu']} "
              f"{'完整' if r['ok'] else '不完整'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
