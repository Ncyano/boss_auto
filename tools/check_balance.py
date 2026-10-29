"""查 DeepSeek / 通义 账户余额，并做一次最小可用的连通性测试。

用法：
    .venv\\Scripts\\python.exe tools\\check_balance.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))


def _post(url: str, key: str, payload: dict, timeout: int = 30) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def check_deepseek(cfg: dict) -> None:
    key = (cfg.get("api_key") or "").strip()
    base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    print("  === DeepSeek ===")
    if not key:
        print("    [NG] 没有配置 api_key")
        return
    # 1) 余额
    try:
        req = urllib.request.Request(f"{base}/user/balance",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            bal = json.loads(r.read().decode("utf-8"))
        info = bal.get("balance_infos") or []
        if info:
            for i in info:
                print(f"    余额: {i.get('total_balance')} {i.get('currency')}"
                      f"（赠金 {i.get('granted_balance')} / 充值 {i.get('topped_up_balance')}）")
        else:
            print(f"    余额接口返回: {json.dumps(bal, ensure_ascii=False)[:120]}")
        if not bal.get("is_available", True):
            print("    [NG] 账户不可用（余额不足）→ 请充值")
    except Exception as exc:
        print(f"    余额查询失败（不影响使用）: {exc}")
    # 2) 实测一次
    code, resp = _post(f"{base}/chat/completions", key,
                       {"model": cfg.get("model") or "deepseek-flash",
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 5})
    if code == 200:
        print("    [OK] 判定模型可调用")
    elif code == 402:
        print("    [NG] 402 余额不足 → 请到 platform.deepseek.com 充值")
    elif code == 401:
        print("    [NG] 401 key 无效 → 检查 api_key")
    else:
        print(f"    [NG] HTTP {code}: {json.dumps(resp, ensure_ascii=False)[:120]}")


def check_qwen(vision: dict) -> None:
    key = (vision.get("api_key") or "").strip()
    base = (vision.get("base_url") or "").rstrip("/")
    print("\n  === 通义千问（OCR）===")
    if not key or not base:
        print("    [NG] 没有配置 api_key / base_url")
        return
    model = vision.get("ocr_model") or "qwen-vl-ocr"
    code, resp = _post(f"{base}/chat/completions", key,
                       {"model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 5})
    if code == 200:
        print(f"    [OK] OCR 模型 {model} 可调用")
    elif code in (401, 403):
        print(f"    [NG] {code} key 无效或无权限")
    elif code == 402:
        print("    [NG] 402 余额不足 → 阿里云百炼控制台充值")
    else:
        print(f"    [NG] HTTP {code}: {json.dumps(resp, ensure_ascii=False)[:120]}")


def main() -> int:
    import settings
    cfg = settings.load()
    check_qwen(cfg.get("vision") or {})
    check_deepseek(cfg.get("judge") or {})
    print("\n  提示：402 就是欠费，充完立刻恢复，不用重装。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
