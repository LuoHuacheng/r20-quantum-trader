"""提取对拍门的**录制式**黄金函数体快照。

## 为什么不再拿 `git show PRE:...` 当基线

第八十四/八十六刀这些门的原始写法是：每次运行都把当前实现与**搬运前**的
`scripts/ai_factor_trader.py`（`PRE` 提交）逐字比对。那是一条 **one-shot** 基线 ——
函数搬走之后一旦被后续提交**有意**修改（例如 aa6d4e0「多所挂单展示 / 修复负数
张数泄漏」给挂单/持仓/对账链路加了多所形态兜底），「与搬运前逐字相同」就永远
不可能再成立，门会永久变红，反而把真实回归淹没。

## 现在的语义

基线 = `tests/extraction/goldens/<name>.json` 里录下的函数体（`ast.dump`，零归一）。
- 有意修改实现 → 重录：`R20_RECORD_EXTRACTION_GOLDENS=1 python -m unittest <本模块>`
  然后把 golden 文件一并提交（diff 里能看清这次到底改了什么）。
- 意外丢行 / 闭包被外提 / 壳注入形状变了 → 门立刻变红。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent / "goldens"


def record_mode() -> bool:
    """`R20_RECORD_EXTRACTION_GOLDENS=1` 时进入录制模式（跑一次即重录并 skip）。"""
    return os.environ.get("R20_RECORD_EXTRACTION_GOLDENS", "").strip() == "1"


def path_for(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def load(name: str) -> dict:
    path = path_for(name)
    if not path.exists():
        raise AssertionError(
            f"缺少黄金快照 {path}；请先 R20_RECORD_EXTRACTION_GOLDENS=1 跑一次本模块重录")
    return json.loads(path.read_text(encoding="utf-8"))


def save(name: str, payload: dict) -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = path_for(name)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path
