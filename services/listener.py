import asyncio
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import AtAll, Image, Plain
from astrbot.core.star import Context

from ..core.data_manager import DataManager
from ..core.douyin import get_live_snapshot, get_user_works
from ..core.models import SubscriptionRecord
from ..core.utils import build_user_url, build_video_url
from .renderer import Renderer


class DouyinListener:
    """抖音后台监听服务 (数据来源: amagi 桥接)"""

    def __init__(
            self,
            context: Context,
            data_manager: DataManager,
            amagi,
            renderer: Renderer,
            cfg: dict
    ):
        self.context = context
        self.data_manager = data_manager
        self.amagi = amagi
        self.renderer = renderer
        self.cfg = cfg

        self.interval_secs = max(10, int(cfg.get("poll_interval", 60)))
        self.enable_live = cfg.get("enable_live_monitor", True)

        self._running = False
        self._video_task: Optional[asyncio.Task] = None
        self._live_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动后台监听"""
        if self._running:
            return
        self._running = True
        logger.info("抖音监听服务已启动")

        # 启动视频监控
        self._video_task = asyncio.create_task(self._video_loop())
        # 启动直播监控
        if self.enable_live:
            self._live_task = asyncio.create_task(self._live_loop())

        # 等待任务（保持运行）
        await asyncio.gather(
            self._video_task,
            self._live_task,
            return_exceptions=True
        )

    async def stop(self):
        """停止后台监听"""
        self._running = False
        if self._video_task and not self._video_task.done():
            self._video_task.cancel()
        if self._live_task and not self._live_task.done():
            self._live_task.cancel()
        logger.info("抖音监听服务已停止")

    def _amagi_ready(self) -> bool:
        """桥接是否可提供服务 (Cookie 已配置且进程已就绪)"""
        return bool(self.amagi.cookie_configured and self.amagi.running and self.amagi.started)

    # ==================== 视频监控 ====================

    async def _video_loop(self):
        """视频监控循环"""
        while self._running:
            try:
                all_subs = self.data_manager.get_all_subscriptions()
                for sub_user, records in all_subs.items():
                    for record in records:
                        if record.sub_type == 'video':
                            await self._check_user_videos(sub_user, record)
                await asyncio.sleep(self.interval_secs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"视频监控循环出错: {e}")
                await asyncio.sleep(10)

    async def _check_user_videos(self, sub_user: str, record: SubscriptionRecord):
        """检查单个用户的视频更新（信任API顺序，遇已知ID停）"""
        sec_uid = record.sec_uid or record.uid
        if not sec_uid:
            return

        try:
            await self.amagi.ensure_started()
        except Exception:
            pass
        if not self._amagi_ready():
            return

        try:
            works = await get_user_works(self.amagi, sec_uid)
            if not works:
                return

            # 构建已知 ID 集合（最后推送的 + 最近缓存的）
            known_ids = set()
            if record.last_video_id:
                known_ids.add(record.last_video_id)
            if record.recent_ids:
                known_ids.update(record.recent_ids)

            # 遍历 API 返回（信任 API 顺序：新→旧），收集新视频直到遇到已知 ID
            new_videos = []
            for w in works:
                wid = str(w.get('aweme_id', ''))
                if not wid:
                    continue
                if wid in known_ids:
                    break  # 遇到已知 ID，后面的都是旧的
                new_videos.append(w)

            # 首次订阅 / 没有已知 ID
            if not record.last_video_id:
                if new_videos:
                    latest = new_videos[0]
                    latest_id = str(latest.get('aweme_id', ''))
                    record.last_video_id = latest_id
                    self.data_manager.update_subscription(
                        sub_user, record.uid, 'video',
                        last_video_id=latest_id,
                        recent_ids=[latest_id],
                        nickname=latest.get('author', {}).get('nickname', record.nickname)
                    )
                    logger.info(f"首次记录用户 {sec_uid} 的最新视频: {latest_id}")
                return

            # 旧数据迁移：有 last_video_id 但 recent_ids 为空（升级前的老数据）
            # → 不推送，只缓存当前最新一批 ID，下次开始正常检测
            if record.last_video_id and not record.recent_ids:
                all_ids = [str(w.get('aweme_id', '')) for w in works if w.get('aweme_id')]
                cache_ids = all_ids[:5]
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'video',
                    recent_ids=cache_ids,
                )
                logger.info(f"旧数据迁移：已缓存 {len(cache_ids)} 个视频ID")
                return

            # 有已知 ID，但没有新视频
            if not new_videos:
                return

            # 有新视频 → 推送
            nickname = new_videos[-1].get('author', {}).get('nickname', record.nickname)
            new_ids = [str(w.get('aweme_id', '')) for w in new_videos]
            latest_id = new_ids[0]

            # 更新记录：last = 最新ID, recent_ids = 最近几条缓存
            updated_recent = list(dict.fromkeys(new_ids + [record.last_video_id] + (record.recent_ids or [])))[:5]
            self.data_manager.update_subscription(
                sub_user, record.uid, 'video',
                last_video_id=latest_id,
                recent_ids=updated_recent,
                nickname=nickname
            )

            # 从旧到新推送
            for work in reversed(new_videos):
                await self._push_video_message(sub_user, record, work)

        except Exception as e:
            logger.error(f"检查用户 {sec_uid} 视频失败: {e}")

    async def _push_video_message(self, sub_user: str, record: SubscriptionRecord, work: dict):
        """推送视频消息"""
        try:
            nickname = work.get('author', {}).get('nickname', record.nickname or record.uid)
            aweme_id = str(work.get('aweme_id', ''))

            # 使用渲染器生成消息（返回文本 + 可选图片）
            text, img_path = await self.renderer.render_video(work, nickname)

            # 构建 MessageChain
            chain = MessageChain()
            if record.at_all:
                chain.at_all()
            if img_path:
                chain.file_image(img_path)
                url = build_video_url(aweme_id)
                chain.message(f"\n{url}")
            else:
                chain.message(text)

            await self.context.send_message(sub_user, chain)
            logger.info(f"已向 {sub_user} 推送视频: {aweme_id}")
        except Exception as e:
            logger.error(f"推送视频消息失败: {e}")

    # ==================== 直播监控 ====================

    async def _live_loop(self):
        """直播监控循环"""
        while self._running:
            try:
                all_subs = self.data_manager.get_all_subscriptions()
                for sub_user, records in all_subs.items():
                    for record in records:
                        if record.sub_type == 'live':
                            await self._check_live_status(sub_user, record)
                await asyncio.sleep(self.interval_secs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"直播监控循环出错: {e}")
                await asyncio.sleep(10)

    async def _check_live_status(self, sub_user: str, record: SubscriptionRecord):
        """按订阅用户检查其直播上下播状态"""
        sec_uid = record.sec_uid or ""
        if not sec_uid:
            logger.debug(f"直播订阅缺少 sec_uid, 跳过: {record.uid}")
            return

        try:
            await self.amagi.ensure_started()
        except Exception:
            pass
        if not self._amagi_ready():
            return

        try:
            snap = await get_live_snapshot(self.amagi, sec_uid)
            if not snap:
                return

            is_now_live = snap["is_live"]
            room_title = snap.get("room_title") or ""

            # 更新昵称
            if snap.get("nickname") and snap["nickname"] != record.nickname:
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    nickname=snap["nickname"],
                )

            if is_now_live and not record.is_live:
                # 开播了！
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    is_live=True,
                    last_live_title=room_title,
                )
                await self._push_live_message(sub_user, record, True, room_title,
                                              extra={"avatar": snap.get("avatar", "")})

            elif not is_now_live and record.is_live:
                # 下播了
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    is_live=False,
                )
                await self._push_live_message(sub_user, record, False, record.last_live_title,
                                              extra={"avatar": snap.get("avatar", "")})

        except Exception as e:
            logger.error(f"检查直播状态失败 (sec_uid={sec_uid}): {e}")

    async def _push_live_message(self, sub_user: str, record: SubscriptionRecord, is_live: bool,
                                 title: str = "", extra: Optional[dict] = None):
        """推送直播消息"""
        extra = extra or {}

        # 使用渲染器生成消息（返回文本 + 可选图片）
        text, img_path = await self.renderer.render_live(
            record, is_live, title,
            avatar=extra.get("avatar", ""),
        )

        try:
            # 构建 MessageChain
            chain = MessageChain()
            if is_live and (record.live_atall or record.at_all):
                chain.at_all()
            if img_path:
                chain.file_image(img_path)
                url = build_user_url(record.sec_uid)
                chain.message(f"\n{url}")
            else:
                chain.message(text)

            await self.context.send_message(sub_user, chain)
            logger.info(f"已向 {sub_user} 推送直播状态: {'开播' if is_live else '下播'}")
        except Exception as e:
            logger.error(f"推送直播消息失败: {e}")
