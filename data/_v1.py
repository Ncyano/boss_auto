import sys, time
sys.path.insert(0, 'boss_vision')
import numpy as np, vlm
from PIL import Image
from pathlib import Path

cfg = vlm.load_config()
print('当前 config 里的模型:', cfg['model'], '| max_image_side', cfg.get('max_image_side'))
shots = sorted(Path('boss_vision/runs').glob('*-shots/*.png'))[:2]
for p in shots:
    bgr = np.asarray(Image.open(p).convert('RGB'))[:, :, ::-1].copy()
    t0 = time.time()
    raw = vlm.read_screen(bgr, cfg, log=lambda *_: None)
    dt = time.time() - t0
    u = raw.get('usage') or {}
    parts = vlm.normalize(raw['sections'])
    print(f'  {p.name}: {dt:.1f}s  token {u.get("total_tokens")}  '
          f'工作 {len(parts["work"])} 段 / 教育 {len(parts["education"])} 条')
