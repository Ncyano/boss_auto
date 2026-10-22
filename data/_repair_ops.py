"""对运营助理数据集做一次补采：读取失败的重读，缺职责的放大重读。"""
import csv, json, sys
from pathlib import Path
ROOT = Path('.').resolve()
sys.path.insert(0, str(ROOT / 'boss_vision'))
import numpy as np, vlm, screening, completeness
from PIL import Image

DATA = ROOT / 'data'
SHOTS = ROOT / 'boss_vision/runs/hr-shots'
cfg = vlm.load_config()
ZOOM = dict(cfg); ZOOM['max_image_side'] = int(cfg.get('max_image_side', 1024)) * 2
ZOOM['retries'] = 4

labels = [r for r in csv.DictReader(open(DATA/'hr-labels.csv', encoding='utf-8-sig'))
          if '运营助理' in (r.get('岗位') or '')]
rows = list(csv.DictReader(open(DATA/'运营助理-数据集.csv', encoding='utf-8-sig')))
have = {(r['招聘人'], r['表内序号']) for r in rows}

missing_rows = [r for r in labels if (r['招聘人'], r['序号']) not in have]
print('缺的记录:', [(r['招聘人'], r['序号']) for r in missing_rows])
incomplete = [r for r in rows if (r['缺失字段'] or '').strip()]
print('缺字段的记录:', [(r['姓名'], r['缺失字段'][:20]) for r in incomplete])

def read_images(images, conf):
    work, education, name, basic = [], [], '', {}
    for p in images:
        bgr = np.asarray(Image.open(p).convert('RGB'))[:, :, ::-1].copy()
        try:
            raw = vlm.read_screen(bgr, conf, log=lambda *_: None)
        except Exception as exc:
            print('   失败:', exc); continue
        if not name: name = vlm.clean_name(raw.get('name') or '')
        if not basic: basic = vlm.normalize_basic(raw)
        mode = vlm.scan_education_mode(bgr)
        if mode: basic['education_mode'] = mode
        parts = vlm.normalize(raw['sections'])
        vlm.merge_screens(work, parts['work'])
        for e in parts['education']:
            same = next((o for o in education if not e.get('period') or not o.get('period')
                         or e['period']==o['period']), None)
            if same is None: education.append(e)
            else:
                for f in ('school','major','degree','period'):
                    if not (same.get(f) or '').strip() and (e.get(f) or '').strip(): same[f]=e[f]
    return {'name': name, 'basic': basic, 'work': work, 'education': education}

fixed = 0
# 1) 补齐缺的记录
for r in missing_rows:
    images = sorted((SHOTS/r['招聘人']).glob(f"r{int(r['序号'])+1}-*"))
    if not images: continue
    print(f"  补读 {r['招聘人']}#{r['序号']}（{len(images)} 张，放大）")
    res = read_images(images, ZOOM)
    if not res['work'] and not res['education']:
        print('   仍失败'); continue
    work = res['work']; education = res['education']
    first = education[0] if education else {}
    ok, missing, nice = completeness.check({'name': res['name'], 'basic': res['basic'],
                                            'education': education, 'work': work,
                                            'collection': {'complete': bool(education)}})
    row = {k: '' for k in csv.DictReader(open(DATA/'运营助理-数据集.csv', encoding='utf-8-sig')).fieldnames}
    row.update({
        '序号': len(rows)+1, '招聘人': r.get('招聘人') or r['招聘人'], '表内序号': r['序号'],
        '姓名': res['name'], '年龄': res['basic'].get('age') or '',
        '学历性质': ('非全日制' if '非全日制' in (res['basic'].get('education_mode') or '')
                 else ('全日制' if '全日制' in (res['basic'].get('education_mode') or '') else '未知')),
        '毕业年份': res['basic'].get('graduation_year') or '',
        '求职状态': res['basic'].get('status') or '',
        '学校': (first.get('school') or '').strip(), '专业': (first.get('major') or '').strip(),
        '学历': (first.get('degree') or '').strip(), '教育起止': (first.get('period') or '').strip(),
        '教育条数': len(education), '工作条数': len(work),
        '工作经历全文': '\n'.join(f'{w.get("period","")} {w.get("company","")}/{w.get("title","")}'
                              + ('：' + '；'.join(w.get('descriptions') or []) if w.get('descriptions') else '')
                              for w in work),
        '缺失字段': '；'.join(missing), '建议补充': '、'.join(nice),
        '采集完整': '是' if ok else '否', '截图张数': len(images),
        '人事结论': r.get('结论') or '',
        '人事原因': (r.get('不合适原因') or r.get('合适原因') or '').replace('；',' / ')})
    for i in range(1, 11):
        w = work[i-1] if i-1 < len(work) else {}
        row[f'第{i}家公司'] = w.get('company',''); row[f'第{i}家职位'] = w.get('title','')
        row[f'第{i}家起止'] = w.get('period','')
    rows.append(row); fixed += 1
    print(f"   补回: {res['name'] or '(无名)'} 工作{len(work)} 教育{len(education)}")

# 2) 缺职责的放大重读
for r in incomplete:
    images = sorted((SHOTS/r['招聘人']).glob(f"r{int(r['表内序号'])+1}-*"))
    if not images: continue
    print(f"  放大重读 {r['姓名'] or '(无名)'}（{len(images)} 张）")
    res = read_images(images, ZOOM)
    if not res['work']: continue
    # 用重读结果补缺失的职责/公司名
    work = res['work']
    changed = False
    for i in range(1, 11):
        company = r.get(f'第{i}家公司',''); title = r.get(f'第{i}家职位','')
        if not (company or title): continue
        match = next((w for w in work if (w.get('company') or '') == company), None)
        if not match: continue
        if not r.get(f'第{i}家公司') and match.get('company'):
            r[f'第{i}家公司'] = match['company']; changed = True
    if changed:
        full = '\n'.join(f"{r.get(f'第{i}家起止','')} {r.get(f'第{i}家公司','')}/{r.get(f'第{i}家职位','')}"
                        for i in range(1,11) if r.get(f'第{i}家公司') or r.get(f'第{i}家职位'))
        r['工作经历全文'] = full
        ok, missing, nice = completeness.check({
            'name': r['姓名'], 'basic': {'age': r['年龄'], 'education_mode': r['学历性质']},
            'education': [{'school': r['学校'], 'degree': r['学历'], 'major': r['专业'],
                           'period': r['教育起止']}],
            'work': [{'company': r.get(f'第{i}家公司',''), 'title': r.get(f'第{i}家职位',''),
                      'period': r.get(f'第{i}家起止',''),
                      'descriptions': [r['工作经历全文']] if r['工作经历全文'] else []}
                     for i in range(1,11) if r.get(f'第{i}家公司') or r.get(f'第{i}家职位')],
            'collection': {'complete': True}})
        r['缺失字段'] = '；'.join(missing); r['采集完整'] = '是' if ok else '否'
        fixed += 1
        print(f"   补字段后: {r['缺失字段'] or '无'}")

if fixed:
    for i, row in enumerate(rows, 1): row['序号'] = str(i)
    with open(DATA/'运营助理-数据集.csv', 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"\n已更新 {len(rows)} 条 -> data/运营助理-数据集.csv")
else:
    print('无需更新')
