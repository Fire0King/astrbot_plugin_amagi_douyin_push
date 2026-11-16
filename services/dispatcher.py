"""
订阅通知的统一发送出口。

集中处理四件事:
  1. 静默模式 —— 机器人长时间没能成功推送后重新上线时(例如停电/断网/重载),
     积压的更新会在恢复瞬间集中涌出。此时先静默一段时间, 只记日志不发送,
     避免一次性刷屏。
  2. 延迟推送 —— 可分别给「私聊」和「群聊」设置延迟秒数(例如群聊延迟 5 分钟),
     直播上下播默认不受延迟影响(开播晚 5 分钟就没意义了)。
     延迟由后台任务执行, 不会阻塞监听循环; 排队期间不会误标记为"已发送"。
  3. 发送结果 —— 发送失败只记日志并返回结果, 不向上抛异常, 避免打断监听循环;
     发送成功则触发回调(用于持久化"上次推送成功时间")。
     图片消息可携带降级链(纯文本+封面), 图片发不出去时自动改发降级内容 ——
     延迟推送时这一步同样生效。
  4. use_t2i(False) —— 主动推送的消息链不再交给文转图服务处理。
     否则一条纯文本推送可能又被转成图片, 绕回同一个 t2i 依赖。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Literal, Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain

NotificationCategory = Literal["video", "live"]
SentHook = Callable[["SubscriptionNotification"], None | Awaitable[None]]

# unified_msg_origin 里的群聊标记, 形如 "qian:GroupMessage:123456"
_GROUP_MESSAGE_TYPE = "GroupMessage"


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
    fallback_chain_parts: Optional[list[Any]] = None
    """发送失败时的降级消息链(通常是纯文本+封面原图); 延迟推送时由发送器自行降级"""


@dataclass(frozen=True)
class DispatchResult:
    """发送结果"""

    sent: bool
    dropped: bool = False
    """是否被主动丢弃(静默模式), 与发送失败区分开: 丢弃时不应再走降级重发"""
    queued: bool = False
    """是否已排入延迟队列(既非成功也非失败): 不要立即重发, 由队列稍后发送"""
    reason: str = ""


class SubscriptionNotificationDispatcher:
    """订阅通知发送器"""

    # 延迟队列上限: 超过后不再排队, 直接发送(宁可迟到也不要丢)
    MAX_PENDING = 200
    # 延迟秒数上限(防止配置写错导致消息永远不来)
    MAX_DELAY_SECS = 3600.0
    # 平台未就绪时的重试间隔(秒)
    RETRY_DELAY_SECS = 3.0

    def __init__(
        self,
        context: Any,
        on_sent: Optional[SentHook] = None,
    ):
        self.context = context
        self.on_sent = on_sent
        self.silent_until_ts = 0
        self._delay_private = 0.0
        self._delay_group = 0.0
        self._live_ignore_delay = True
        self._pending: set[asyncio.Task] = set()

    # ---------------- 延迟配置 ----------------

    def set_delays(
        self,
        private_secs: Any = 0,
        group_secs: Any = 0,
        live_ignore_delay: bool = True,
    ) -> None:
        """设置私聊/群聊推送延迟(秒)。0 表示立即推送。

        改配置后无需重启: 下一条通知就按新值走(已排队的不受影响)。
        """
        old = (self._delay_private, self._delay_group, self._live_ignore_delay)
        self._delay_private = self._clean_delay(private_secs)
        self._delay_group = self._clean_delay(group_secs)
        self._live_ignore_delay = bool(live_ignore_delay)
        new = (self._delay_private, self._delay_group, self._live_ignore_delay)
        if new != old:
            logger.info(
                f"推送延迟已设置: 私聊 {self._delay_private:.0f}s / 群聊 {self._delay_group:.0f}s "
                f"/ 直播{'不受影响' if self._live_ignore_delay else '同样延迟'}"
            )
        else:
            logger.info(
                f"推送延迟: 私聊 {self._delay_private:.0f}s / 群聊 {self._delay_group:.0f}s / "
                f"直播{'不受影响' if self._live_ignore_delay else '同样延迟'}"
            )

    def _clean_delay(self, value: Any) -> float:
        try:
            secs = float(value)
        except (TypeError, ValueError):
            return 0.0
        if secs <= 0:
            return 0.0
        return min(secs, self.MAX_DELAY_SECS)

    @property
    def delay_private(self) -> float:
        return self._delay_private

    @property
    def delay_group(self) -> float:
        return self._delay_group

    @property
    def live_ignore_delay(self) -> bool:
        return self._live_ignore_delay

    @staticmethod
    def is_group_session(sub_user: str) -> bool:
        """是否群聊会话(UMO 形如 qian:GroupMessage:123)"""
        return f":{_GROUP_MESSAGE_TYPE}:" in (sub_user or "")

    def delay_for(self, notification: SubscriptionNotification) -> float:
        """该通知需要延迟多少秒"""
        if notification.category == "live" and self._live_ignore_delay:
            return 0.0
        return self._delay_group if self.is_group_session(notification.sub_user) else self._delay_private

    def pending_count(self) -> int:
        """当前排队中的延迟推送数量"""
        return len(self._pending)

    def cancel_pending(self) -> int:
        """取消所有排队中的延迟推送(插件卸载时调用), 返回取消数量"""
        tasks = [t for t in self._pending if not t.done()]
        for task in tasks:
            task.cancel()
        return len(tasks)

    # ---------------- 发送 ----------------

    async def publish(self, notification: SubscriptionNotification) -> DispatchResult:
        """发送一条通知(可能排入延迟队列)。失败只记日志, 不抛异常。"""
        if self._is_silent(notification):
            return DispatchResult(sent=False, dropped=True, reason="silent_mode")

        delay = self.delay_for(notification)
        if delay <= 0:
            return await self._send(notification)

        if len(self._pending) >= self.MAX_PENDING:
            logger.warning(
                f"延迟队列已满({self.MAX_PENDING}), 本条立即发送: "
                f"sub_user={notification.sub_user} content_id={notification.content_id}"
            )
            return await self._send(notification)

        task = asyncio.create_task(self._send_later(notification, delay))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        logger.info(
            f"推送已排入延迟队列: sub_user={notification.sub_user} "
            f"category={notification.category} content_id={notification.content_id} "
            f"约 {delay:.0f} 秒后发送"
        )
        return DispatchResult(sent=False, queued=True, reason=f"queued_{delay:.0f}s")

    async def _send_later(self, notification: SubscriptionNotification, delay: float) -> None:
        """延迟发送(后台任务)。到期时重新检查静默期, 并自行处理降级。"""
        try:
            await asyncio.sleep(delay)
            if self._is_silent(notification):
                logger.info(
                    f"延迟推送到期但处于静默期, 丢弃: sub_user={notification.sub_user} "
                    f"content_id={notification.content_id}"
                )
                return
            result = await self._send(notification)
            if result.sent:
                logger.info(
                    f"延迟推送已送达: sub_user={notification.sub_user} "
                    f"content_id={notification.content_id}"
                )
            elif not result.dropped:
                logger.warning(
                    f"延迟推送未送达: sub_user={notification.sub_user} "
                    f"content_id={notification.content_id} reason={result.reason}"
                )
        except asyncio.CancelledError:
            logger.info(f"延迟推送被取消: content_id={notification.content_id}")
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"延迟推送异常: sub_user={notification.sub_user} error={e!r}")

    async def _send(self, notification: SubscriptionNotification) -> DispatchResult:
        """立即发送; 失败时若带降级链则改发降级内容。"""
        result = await self._send_chain(notification, notification.chain_parts)
        if result.sent or result.dropped or not notification.fallback_chain_parts:
            return result
        logger.warning(
            f"原消息发送失败, 降级重发: sub_user={notification.sub_user} "
            f"content_id={notification.content_id} reason={result.reason}"
        )
        return await self._send_chain(notification, notification.fallback_chain_parts)

    async def _send_chain(
        self, notification: SubscriptionNotification, parts: list[Any]
    ) -> DispatchResult:
        chain = MessageChain(chain=list(parts)).use_t2i(False)
        ok: Any = None
        for attempt in range(2):
            try:
                ok = await self.context.send_message(notification.sub_user, chain)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"发送订阅通知失败: sub_user={notification.sub_user} "
                    f"category={notification.category} content_id={notification.content_id} "
                    f"error={e!r}"
                )
                return DispatchResult(sent=False, reason=str(e) or type(e).__name__)

            if ok is not False:
                break
            # 平台还没连上时 send_message 会返回 False（不抛异常），
            # 最常见于刚重启、平台适配器尚未就绪 —— 等几秒重试一次
            if attempt == 0:
                logger.warning(
                    f"平台未就绪, {self.RETRY_DELAY_SECS:.0f} 秒后重试发送: "
                    f"sub_user={notification.sub_user} category={notification.category}"
                )
                await asyncio.sleep(self.RETRY_DELAY_SECS)

        if ok is False:
            logger.warning(
                f"发送订阅通知未送达(平台未就绪?): sub_user={notification.sub_user} "
                f"category={notification.category} content_id={notification.content_id}"
            )
            return DispatchResult(sent=False, reason="platform_not_ready")

        await self._on_sent(notification)
        return DispatchResult(sent=True)

    # ---------------- 静默 ----------------

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
