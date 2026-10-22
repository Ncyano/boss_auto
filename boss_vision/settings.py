"""统一的配置入口：所有 API 调用（简历识图 / 经历判断）都从这里读。

配置文件位置（按优先级）：
    1. 环境变量 BOSS_CONFIG 指向的路径
    2. 仓库根目录 config.json
    3. 都不存在时，用内置默认值并生成一份 config.json（不含密钥）

**config.json 不进仓库**（.gitignore 已排除），仓库里只有 config.example.json 模板。

结构：
{
  "vision": { ... 千问 VL 读简历截图 ... },
  "judge":  { ... DeepSeek 判工作经历 ... },
  "paths":  { ... 可选：数据目录 ... }
}

密钥优先从环境变量取（vision.api_key_env / judge.api_key_env），其次用配置里的 api_key。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = "BOSS_CONFIG"

DEFAULTS: dict = {
    "vision": {
        "_说明": "读在线简历截图的多模态模型（千问 VL）",
        "provider": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-vl-plus",
        "api_key_env": "VLM_API_KEY",
        "api_key": "",
        "temperature": 0,
        "max_tokens": 2000,
        "timeout": 120,
        "retries": 2,
        "max_image_side": 1024,
    },
    "judge": {
        "_说明": "判断工作经历是否匹配岗位的文本模型（DeepSeek）",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",
        "temperature": 0,
        "max_tokens": 2000,
        "timeout": 120,
        "retries": 3,
        "thinking": "disabled",
    },
    "paths": {
        "data_dir": "data",
        "kb_dir": "运营助理知识库_v0.1",
    },
}

# 旧的分散配置：首次启动时自动迁移进来
LEGACY = {
    "vision": ROOT / "data" / "vlm-config.json",
    "judge": ROOT / "data" / "llm-config.json",
}


def config_path() -> Path:
    override = os.environ.get(ENV_PATH)
    return Path(override) if override else ROOT / "config.json"


def _migrate_legacy() -> dict:
    """把 data/vlm-config.json、data/llm-config.json 合并成一份。"""
    merged: dict = {}
    for section, path in LEGACY.items():
        if not path.exists():
            continue
        try:
            merged[section] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return merged


def load(create: bool = True) -> dict:
    """返回完整配置（默认值 + 文件 + 旧配置迁移）。"""
    cfg = json.loads(json.dumps(DEFAULTS))
    path = config_path()

    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] {path.name} 解析失败，使用默认配置：{exc}")
            user = {}
        for section, values in user.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update({k: v for k, v in values.items() if not k.startswith("_")})
            else:
                cfg[section] = values
    else:
        legacy = _migrate_legacy()
        for section, values in legacy.items():
            if isinstance(cfg.get(section), dict):
                cfg[section].update({k: v for k, v in values.items() if not k.startswith("_")})
        if legacy and create:
            save(cfg)
            print(f"[info] 已从旧配置迁移生成 {path.name}（旧文件可以删掉）")
        elif create:
            save(cfg)

    return cfg


def save(cfg: dict, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # 尽量收紧权限，避免被别的程序读到密钥
        os.chmod(target, 0o600)
    except Exception:
        pass
    return target


def section(name: str, cfg: dict | None = None) -> dict:
    return (cfg or load()).get(name) or {}


def api_key(name: str, cfg: dict | None = None) -> str:
    """取某一段的密钥：环境变量优先，其次配置文件。"""
    sec = section(name, cfg)
    env_name = sec.get("api_key_env") or ""
    if env_name:
        value = os.environ.get(env_name, "")
        if value:
            return value.strip()
    return str(sec.get("api_key") or "").strip()


def available(name: str) -> bool:
    return bool(api_key(name))


def example() -> dict:
    """给 config.example.json 用：去掉密钥与内部说明。"""
    cfg = json.loads(json.dumps(DEFAULTS))
    for name, sec in cfg.items():
        if isinstance(sec, dict):
            sec.pop("_说明", None)
            if "api_key" in sec:
                sec["api_key"] = ""
    return cfg


def self_test() -> int:
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print("[1] 默认配置")
    cfg = load(create=False)
    check("有 vision 段", "vision" in cfg)
    check("有 judge 段", "judge" in cfg)
    check("vision 默认千问", "dashscope" in cfg["vision"]["base_url"], cfg["vision"]["base_url"])
    check("judge 默认 deepseek", "deepseek" in cfg["judge"]["base_url"], cfg["judge"]["base_url"])

    print("\n[2] 密钥取值优先级")
    os.environ["VLM_API_KEY"] = "env-key-123"
    try:
        check("环境变量优先", api_key("vision", cfg) == "env-key-123", api_key("vision", cfg)[:6])
    finally:
        os.environ.pop("VLM_API_KEY", None)
    fallback = {"vision": {"api_key": "cfg-key", "api_key_env": "NO_SUCH_ENV"}}
    check("无环境变量时用配置", api_key("vision", fallback) == "cfg-key")

    print("\n[3] 路径与模板")
    check("默认路径在仓库根", config_path().name in ("config.json", os.environ.get(ENV_PATH, "")))
    ex = example()
    check("模板不含密钥", all(not (sec.get("api_key") or "") for sec in ex.values()
                              if isinstance(sec, dict)))
    check("模板不含说明", all("_说明" not in sec for sec in ex.values() if isinstance(sec, dict)))

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
