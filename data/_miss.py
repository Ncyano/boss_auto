import csv, sys
sys.path.insert(0, "boss_vision"); sys.path.insert(0, "tools")
import screening, train_eval

rows = list(csv.DictReader(open("data/dataset/train.csv", encoding="utf-8-sig")))
rules = screening.load_rules()
bad = []
for i, raw in enumerate(rows, 1):
    srow = train_eval.screen_row_of(raw)
    try:
        res = screening.screen_row(srow, rules)
    except Exception:
        continue
    got, truth = res.get("最终结论"), (raw.get("人事结论") or "").strip()
    st = res.get("处理状态") or ""
    if st in ("采集不完整", "调用失败") or got not in ("合适", "不合适"):
        continue
    if got != truth:
        bad.append((raw.get("姓名") or "无名", truth, got, (res.get("原因") or "")[:60],
                    (raw.get("人事原因") or "")[:50]))

print(f"训练集 {len(rows)} 条，判错 {len(bad)} 条\n")
for nm, t, g, mr, hr in bad:
    print(f"  {nm[:8]:<9} 人工={t:<4} 模型={g:<4}")
    print(f"      模型：{mr}")
    print(f"      人事：{hr}")
