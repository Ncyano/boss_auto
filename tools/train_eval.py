"""训练 + 调参 + 测试评估（70/15/15），按正确分层协议。

    python tools\\train_eval.py

协议（与知识库 precedence 一致）：
    ① 每条记录先跑硬规则（kb.evaluate）：学历/年龄/空档/同行/应届/经验/无经历
       - 判死 -> 直接"不合格"，不进入模型
       - 待复核 -> 记"待复核"
    ② 只有硬规则全部通过的记录才交给 DeepSeek 判工作经历
    ③ 最终结论 = 硬规则结论 + 模型结论

    训练集：只用于挑少样本示例（且只挑硬规则已通过的样本）
    验证集：在 0/3/6 条示例里选最优配置
    测试集：一次性最终评估

产出：data\\train-report.json / data\\train-report.md
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "boss_vision"))
sys.path.insert(0, str(ROOT / "tools"))

import judge  # noqa: E402
import kb as kbmod  # noqa: E402
import screening  # noqa: E402
from build_fewshot import main as build_fs  # noqa: E402

DATA = ROOT / "data"
SET_DIR = DATA / "dataset"
REPORT_JSON = DATA / "train-report.json"
REPORT_MD = DATA / "train-report.md"
SLOTS = 6


def work_text(row: dict) -> str:
    full = (row.get("工作经历全文") or "").strip()
    if full:
        return full
    return (row.get("全部工作经历") or "").strip() or "（简历里没有工作经历）"


def load_rows(name: str) -> list[dict]:
    path = SET_DIR / f"{name}.csv"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，先跑 tools\\split_dataset.py")
    return list(csv.DictReader(open(path, encoding="utf-8-sig")))


def screen_row_of(row: dict) -> dict:
    out = {k: row.get(k, "") for k in ("序号", "姓名", "学校", "学历", "专业",
                                       "学历性质", "年龄", "求职状态", "工作经历条数")}
    for i in range(1, SLOTS + 1):
        for suffix in ("公司", "职位", "起止"):
            key = f"第{i}家{suffix}"
            out[key] = row.get(key, "")
    out.update({"采集完成": "是", "需复核": "否", "复核原因": "", "冲突数": 0})
    return out


def run_split(name: str, rows: list[dict], kb: dict, cfg: dict, shots: list[dict],
              log_each: bool = True) -> dict:
    detail = []
    hard_reject = hard_wrong = 0
    tp = fp = tn = fn = unsure = failed = 0
    tokens = 0

    for i, row in enumerate(rows, 1):
        actual = (row.get("人事结论") or "").strip()
        label = row.get("姓名") or "(无名)"
        kb_out = screening.evaluate_kb(screen_row_of(row), kb)

        if kb_out["verdict"] == "不合适":
            hard_reject += 1
            pred = "不合适"
            reason = "硬规则：" + "；".join(f["reason"] for f in kb_out["failures"])[:70]
            if actual != "不合适":
                hard_wrong += 1
        else:
            try:
                res = judge.judge(work_text(row), kb, cfg, shots=shots)
                tokens += int((res.get("usage") or {}).get("total_tokens") or 0)
                pred = "合适" if res["recommendation"] == "建议合适" else "不合适"
                reason = res["decision_basis"][:70]
            except Exception as exc:
                failed += 1
                pred, reason = "调用失败", str(exc)[:70]

        if pred == "不合适" and actual == "不合适":
            tp += 1
        elif pred == "不合适" and actual == "合适":
            fp += 1
        elif pred == "合适" and actual == "合适":
            tn += 1
        elif pred == "合适" and actual == "不合适":
            fn += 1
        elif pred == "调用失败":
            unsure += 1

        mark = "✓" if pred == actual else ("?" if pred == "调用失败" else "✗")
        if log_each:
            print(f"    [{i}/{len(rows)}] {mark} {label:<8} 真实={actual or '—':<4} "
                  f"预测={pred:<5} {reason[:36]}")
        detail.append({"姓名": row.get("姓名"), "真实": actual, "预测": pred,
                       "理由": reason, "硬规则": kb_out["verdict"] or "通过"})

    judged = tp + fp + tn + fn
    m = {
        "n": len(rows), "judged": judged, "unsure": unsure, "failed": failed,
        "hard_reject": hard_reject, "hard_wrong": hard_wrong,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "tokens": tokens,
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "accuracy": round((tp + tn) / judged, 3) if judged else None,
        "coverage": round(judged / len(rows), 3) if rows else None,
        "hard_precision": (round((hard_reject - hard_wrong) / hard_reject, 3)
                           if hard_reject else None),
    }
    return {"name": name, "metrics": m, "detail": detail}


def build_shots(n: int) -> list[dict]:
    with open(DATA / "_fewshot.log", "w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
        build_fs(["--n", str(n)])
    return judge.load_fewshot()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--shots", default="0,3,6")
    args = ap.parse_args(argv)

    kb = kbmod.load()
    cfg = judge.load_config()
    if not judge.available():
        print("没有 DEEPSEEK_API_KEY")
        return 2

    shot_sizes = [int(x) for x in args.shots.split(",") if x.strip().isdigit()]
    train_rows, val_rows, test_rows = load_rows("train"), load_rows("val"), load_rows("test")

    print(f"知识库 v{kb.get('version')}  模型 {cfg['model']}（少样本+提示词，非微调）")
    print(f"数据：train {len(train_rows)} / val {len(val_rows)} / test {len(test_rows)}\n")

    report = {"time": datetime.now().isoformat(timespec="seconds"), "model": cfg["model"],
              "kb_version": kb.get("version"),
              "sizes": {"train": len(train_rows), "val": len(val_rows), "test": len(test_rows)},
              "stages": []}

    print("[1] 验证集选择示例规模")
    val_results = []
    for n in shot_sizes:
        shots = build_shots(n)
        print(f"  --- 示例 {len(shots)} 条 ---")
        res = run_split("val", val_rows, kb, cfg, shots)
        res["shots"] = len(shots)
        val_results.append(res)
        m = res["metrics"]
        print(f"    硬规则判死 {m['hard_reject']}（错 {m['hard_wrong']}）  模型判 {m['judged']}  "
              f"准确率 {m['accuracy']}  精确率 {m['precision']}  覆盖率 {m['coverage']}\n")

    def key(r):
        m = r["metrics"]
        return (m["accuracy"] or 0, m["coverage"] or 0, -r["shots"])

    best = max(val_results, key=key)
    report["stages"].append({"stage": "val", "results": val_results,
                             "chosen_shots": best["shots"]})
    print(f"  => 选定 {best['shots']} 条示例（验证集准确率 {best['metrics']['accuracy']}）\n")

    if args.quick:
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("--quick：跳过测试集")
        return 0

    shots = build_shots(best["shots"])

    print(f"[2] 测试集一次性评估（示例 {len(shots)} 条）")
    test_res = run_split("test", test_rows, kb, cfg, shots)
    report["stages"].append({"stage": "test", "results": [test_res]})
    tm = test_res["metrics"]

    print("\n[3] 训练集对照")
    train_res = run_split("train", train_rows, kb, cfg, shots, log_each=False)
    report["stages"].append({"stage": "train", "results": [train_res]})
    trm = train_res["metrics"]

    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 运营助理 判断模型 训练/评估报告", "",
        f"- 时间：{report['time']}",
        f"- 模型：{report['model']}（DeepSeek；**非权重微调**，少样本示例 + 提示词校准）",
        f"- 知识库：v{report['kb_version']}",
        f"- 切分：train {len(train_rows)} / val {len(val_rows)} / test {len(test_rows)}"
        "（70/15/15，按人分组防泄漏）",
        "", "## 验证集：示例规模选择", "",
        "| 示例数 | 硬规则判死 | 模型判 | 准确率 | 精确率 | 覆盖率 | token |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in val_results:
        m = r["metrics"]
        lines.append(f"| {r['shots']} | {m['hard_reject']} | {m['judged']} | {m['accuracy']} | "
                     f"{m['precision']} | {m['coverage']} | {m['tokens']} |")
    lines += ["", f"选定 **{best['shots']} 条示例**。", "", "## 测试集（一次性）", "",
              f"- 硬规则判死：{tm['hard_reject']} 条（其中与人事不一致 {tm['hard_wrong']}）",
              f"- 模型判定：{tm['judged']} 条（待复核 {tm['unsure']} / 失败 {tm['failed']}）",
              f"- 准确率：{tm['accuracy']}　精确率：{tm['precision']}　召回率：{tm['recall']}",
              f"- 覆盖率：{tm['coverage']}",
              f"- 混淆：TP={tm['tp']} FP={tm['fp']} TN={tm['tn']} FN={tm['fn']}",
              f"- token：{tm['tokens']}",
              "", "## 训练集（对照，看是否过拟合）", "",
              f"- 准确率 {trm['accuracy']}　精确率 {trm['precision']}　覆盖率 {trm['coverage']}",
              "", "## 测试集逐条", "", "| 姓名 | 真实 | 预测 | 路径 | 依据 |",
              "|---|---|---|---|---|"]
    for d in test_res["detail"]:
        lines.append(f"| {d.get('姓名') or '—'} | {d['真实']} | {d['预测']} | "
                     f"{d['硬规则']} | {d['理由'][:40]} |")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(f"  测试集：准确率 {tm['accuracy']}  精确率 {tm['precision']}  "
          f"召回率 {tm['recall']}  覆盖率 {tm['coverage']}")
    print(f"  训练集：准确率 {trm['accuracy']}  覆盖率 {trm['coverage']}")
    print(f"\n报告 -> {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
