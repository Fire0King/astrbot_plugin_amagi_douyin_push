"""
插件自己的抖音凭据存储。

为什么不把扫码拿到的 Cookie 只写进配置:
  配置会被 AstrBot 在升级插件、改 schema、WebUI 保存设置时**整体重写**。
  2026-10-20 就发生过 `douyin_cookie` 被重写成空值, 而插件进程里还留着旧值,
  直到几天后插件重载才暴露 —— 表现为"amagi 桥接未就绪 / cookie=未配置"。
  所以把凭据单独放在插件数据目录(权限 0600), **优先级高于配置**, 不会因为
  配置被重写而丢。配置文件里的 `douyin_cookie` 仍然可用(作为兜底)。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger
from astrbot.api.star import StarTools

_PLUGIN_NAME = "astrbot_plugin_amagi_douyin_push"
_FILE_NAME = "credential.json"


class CredentialStore:
    """扫码登录得到的凭据 (json, 0600)。内容形如:
    {"cookie": "sessionid=...; ttwid=...; ...", "source": "qr_login",
     "nickname": "又千", "saved_at": 1790000000}
    """

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            base = Path(StarTools.get_data_dir(plugin_name=_PLUGIN_NAME))
            path = base / _FILE_NAME
        self.path = Path(path)

    # ---------------- 读 ----------------

    def load(self) -> Dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as e:  # noqa: BLE001
            logger.warning(f"读取凭据文件失败 ({self.path}): {e}")
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"凭据文件解析失败 ({self.path}): {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def get_cookie(self) -> str:
        return str(self.load().get("cookie") or "").strip()

    def source(self) -> str:
        return str(self.load().get("source") or "")

    def nickname(self) -> str:
        return str(self.load().get("nickname") or "")

    def saved_at(self) -> int:
        try:
            return int(self.load().get("saved_at") or 0)
        except (TypeError, ValueError):
            return 0

    # ---------------- 写 ----------------

    def save_cookie(self, cookie: str, source: str = "qr_login",
                    nickname: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        cookie = (cookie or "").strip()
        if not cookie:
            raise ValueError("cookie 不能为空")
        data: Dict[str, Any] = {
            "cookie": cookie,
            "source": source,
            "nickname": nickname,
            "saved_at": int(time.time()),
        }
        if extra:
            data.update(extra)
        self._write(data)
        logger.info(f"已保存抖音凭据 (来源={source}, 长度={len(cookie)}, 昵称={nickname or '未知'})")

    def clear(self) -> None:
        try:
            self.path.unlink()
            logger.info("已清除保存的抖音凭据")
        except FileNotFoundError:
            return
        except OSError as e:  # noqa: BLE001
            logger.warning(f"清除凭据失败: {e}")

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
