"""
订阅通知的统一发送出口。

集中处理三件事:
  1. 静默模式 —— 机器人长时间没能成功推送后重新上线时(例如停电/断网/重载),
     积压的更新会在恢复瞬间集中涌出。此时先静默一段时间, 只记日志不发送,
     避免一次性刷屏。
  2. 发送结果 —— 发送失败只记日志并返回结果, 不向上抛异常, 避免打断监听循环;
     发送成功则触发回调(用于持久化"上次推送成功时间")。
  3. use_t2i(False) —— 主动推送的消息链不再交给文转图服务处理。
     否则一条纯文本推送可能又被转成图片, 绕回同一个 t2i 依赖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Literal, Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain

NotificationCategory = Literal["video", "live"]
SentHook = Callable[["SubscriptionNotification"], None | Awaitable[None]]


@dataclass(frozen=True)
class SubscriptionNotification:
    """一条待推送的订阅通知"""

    sub_user: str
    """unified_msg_origin, 目标会话"""
    chain_parts: list[Any]
    """消息组件列表(Image/File/Plain/AtAll...)"""
    category: NotificationCategory = "video"
    """通知类型: video(视频更新) / live(直播上下播)"""
    content_id: Optional[str] = None
    """内容标识(如 aweme_id), 仅用于日志排查"""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    """发送结果"""

    sent: bool
    dropped: bool = False
    """是否被主动丢弃(静默模式), 与发送失败区分开: 丢弃时不应再走降级重发"""
    reason: str = ""


class SubscriptionNotificationDispatcher:
    """订阅通知发送器"""

    def __init__(
        self,
        context: Any,
        on_sent: Optional[SentHook] = None,
    ):
        self.context = context
        self.on_sent = on_sent
        self.silent_until_ts = 0

    async def publish(self, notification: SubscriptionNotification) -> DispatchResult:
        """发送一条通知。失败只记日志, 不抛异常。"""
        if self._is_silent(notification):
            return DispatchResult(sent=False, dropped=True, reason="silent_mode")

        chain = MessageChain(chain=list(notification.chain_parts)).use_t2i(False)
        try:
            await self.context.send_message(notification.sub_user, chain)
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"发送订阅通知失败: sub_user={notification.sub_user} "
                f"category={notification.category} content_id={notification.content_id} "
                f"error={e}"
            )
            return DispatchResult(sent=False, reason=str(e))

        await self._on_sent(notification)
        return DispatchResult(sent=True)

    def set_silent_until_ts(self, silent_until_ts: int) -> None:
        """设置静默截止时间戳(秒), 0 表示不静默"""
        self.silent_until_ts = max(int(silent_until_ts), 0)

    def is_silent(self) -> bool:
        """当前是否处于静默期"""
        return int(time.time()) < self.silent_until_ts

    def silent_remaining_secs(self) -> int:
        """距离静默结束还有多少秒"""
        return max(self.silent_until_ts - int(time.time()), 0)

    async def _on_sent(self, notification: SubscriptionNotification) -> None:
        hook = self.on_sent
        if hook is None:
            return
        result = hook(notification)
        if isawaitable(result):
            await result

    def _is_silent(self, notification: SubscriptionNotification) -> bool:
        if notification.category not in ("dynamic", "video", "live"):
            return False
        if int(time.time()) >= self.silent_until_ts:
            return False
        logger.info(
            f"订阅通知被静默丢弃: sub_user={notification.sub_user} "
            f"category={notification.category} content_id={notification.content_id}"
        )
        return True
