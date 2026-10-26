"""
Cookie 规范化与校验。

为什么需要:
  用户从下载器/脚本导出的 cookie 常常是 **JSON 对象**（{"sessionid": "...", ...}），
  整段贴进插件配置后，会被当成"一个 cookie 字段"发给抖音 —— 抖音只回空内容，
  amagi 于是报"抖音数据获取失败 / 你的抖音ck可能已经失效"。
  2026-10-24 那次"连续 851 次失败"就是这个原因：cookie 是 JSON，
  而本地下载器本身能读 JSON，所以看起来像"cookie 没过期"。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from astrbot.api import logger

# 判断"是否已登录"的关键字段：没有 sessionid 的 cookie 只能看公开内容
_REQUIRED_LOGIN_KEYS = ("sessionid",)


def parse_cookie_string(text: str) -> Dict[str, str]:
    """把 `a=1; b=2` 解析成 dict（重复键后者覆盖）。"""
    out: Dict[str, str] = {}
    for item in (text or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def _pairs_from_json(text: str) -> Optional[List[str]]:
    try:
        obj: Any = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    pairs: List[str] = []
    if isinstance(obj, dict):
        pairs = [f"{k}={v}" for k, v in obj.items() if k and v not in (None, "")]
    elif isinstance(obj, list):
        pairs = [
            f"{item.get('name')}={item.get('value')}"
            for item in obj
            if isinstance(item, dict) and item.get("name") and item.get("value")
        ]
    return pairs or None


def normalize_cookie(raw: str) -> str:
    """把各种粘贴形态统一成 `k=v; k=v`。

    支持:
      - 标准 cookie 串: `a=1; b=2`
      - JSON 对象:       `{"a": "1", "b": "2"}`
      - JSON 数组:       `[{"name": "a", "value": "1"}, ...]`
    解析失败时原样返回（不丢用户数据）。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("{") or text.startswith("["):
        pairs = _pairs_from_json(text)
        if pairs:
            logger.info(f"Cookie 是 JSON 格式，已自动转成 k=v 形式（{len(pairs)} 个字段）")
            text = "; ".join(pairs)
        else:
            logger.warning("Cookie 看起来是 JSON 但解析失败，将按原样使用")
    names = parse_cookie_string(text)
    missing = [k for k in _REQUIRED_LOGIN_KEYS if not names.get(k)]
    if missing:
        logger.warning(
            f"⚠️ 抖音 Cookie 缺少 {', '.join(missing)} —— 等于未登录状态，"
            f"接口会返回空数据。请确认复制的是**已登录**状态下的完整 Cookie，或用 /dy_login 扫码登录。"
        )
    return text
