import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.message_components import AtAll, File, Image, Plain
from astrbot.core.star import Context

from ..core.data_manager import DataManager
from ..core.douyin import (
    get_aweme_id,
    get_create_time,
    get_live_snapshot,
    get_user_works,
    is_pinned,
)
from ..core.models import SubscriptionRecord
from ..core.utils import (
    build_user_url,
    build_video_url,
    first_url,
    is_height_valid,
)
from .dispatcher import SubscriptionNotification, SubscriptionNotificationDispatcher
from .renderer import Renderer

# 「桥接未就绪」提示的最小间隔(秒), 避免每个轮询周期刷屏
_NOT_READY_LOG_INTERVAL = 300

# 渲染结果缓存条数上限(按内容 ID 缓存, 多个会话订阅同一主播时复用)
_RENDER_CACHE_LIMIT = 32

# @全体成员可用性判定: 剩余次数的最小值
_MIN_AT_ALL_REMAINING = 1

# 会话类型: 只有群聊才可能 @全体
_GROUP_MESSAGE_TYPE = "GroupMessage"


class DouyinListener:
    """抖音后台监听服务 (数据来源: amagi 桥接)"""

    def __init__(
            self,
            context: Context,
            data_manager: DataManager,
            amagi,
            renderer: Renderer,
            cfg: dict,
            dispatcher: Optional[SubscriptionNotificationDispatcher] = None,
    ):
        self.context = context
        self.data_manager = data_manager
        self.amagi = amagi
        self.renderer = renderer
        self.cfg = cfg
        self.dispatcher = dispatcher or SubscriptionNotificationDispatcher(context)

        self.interval_secs = max(10, int(cfg.get("poll_interval", 60)))
        self.enable_live = cfg.get("enable_live_monitor", True)

        # 渲染结果缓存: content_id -> (文本, 图片路径)
        self._render_cache: "OrderedDict[str, Tuple[str, Optional[str]]]" = OrderedDict()
        self._render_cache_limit = max(
            1, int(cfg.get("render_cache_limit", _RENDER_CACHE_LIMIT))
        )

        self._running = False
        self._video_task: Optional[asyncio.Task] = None
        self._live_task: Optional[asyncio.Task] = None

        # 运行状态 (供 /dy_status 诊断)
        self.last_video_scan_at: float = 0.0
        self.last_live_scan_at: float = 0.0
        self.last_error: str = ""
        self._last_not_ready_log_at: float = 0.0

    # ==================== 生命周期 ====================

    async def start(self):
        """
        启动后台监听 (幂等)。

        注意: 判定「是否已在运行」必须以任务是否结束为准, 不能用 _running 标志 ——
        任务被 cancel() 后标志可能仍是 True, 会导致新任务立即 return 而静默失效。
        """
        if self._video_task and not self._video_task.done():
            logger.debug("抖音监听服务已在运行, 跳过重复启动")
            return

        self._running = True
        logger.info(
            f"抖音监听服务已启动 (间隔 {self.interval_secs}s, "
            f"直播监控 {'开启' if self.enable_live else '关闭'})"
        )

        self._video_task = asyncio.create_task(self._video_loop())
        tasks = [self._video_task]
        if self.enable_live:
            self._live_task = asyncio.create_task(self._live_loop())
            tasks.append(self._live_task)

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._running = False

    async def stop(self):
        """停止后台监听 (等待任务真正结束, 避免与重启竞态)"""
        self._running = False
        for task in (self._video_task, self._live_task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug(f"监听任务结束异常: {e}")
        self._video_task = None
        self._live_task = None
        logger.info("抖音监听服务已停止")

    async def restart(self):
        """重启监听: 先确认旧任务完全结束, 再启动新任务"""
        await self.stop()
        await self.start()

    @property
    def running(self) -> bool:
        """监听循环是否真的在跑"""
        return bool(self._video_task and not self._video_task.done())

    def status_info(self) -> dict:
        now = time.time()

        def _ago(ts: float) -> str:
            if not ts:
                return "尚未扫描"
            return f"{int(now - ts)} 秒前"

        return {
            "running": self.running,
            "interval": self.interval_secs,
            "enable_live": bool(self.enable_live),
            "last_video_scan": _ago(self.last_video_scan_at),
            "last_live_scan": _ago(self.last_live_scan_at),
            "last_error": self.last_error,
        }

    def _amagi_ready(self) -> bool:
        """桥接是否可提供服务 (Cookie 已配置且进程已就绪)"""
        return bool(self.amagi.cookie_configured and self.amagi.running and self.amagi.started)

    def _log_not_ready(self):
        """桥接不可用时给出可见原因 (限流, 避免刷屏)"""
        now = time.time()
        if now - self._last_not_ready_log_at < _NOT_READY_LOG_INTERVAL:
            return
        self._last_not_ready_log_at = now
        info = self.amagi.status_info()
        reason = info.get("last_error") or info.get("build_msg") or "未知"
        logger.warning(
            "amagi 桥接未就绪, 本轮跳过检查 "
            f"(cookie={'已配置' if info.get('cookie_configured') else '未配置'}, "
            f"running={info.get('running')}, started={info.get('started')}, 原因: {reason})"
        )

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
                self.last_video_scan_at = time.time()
                await asyncio.sleep(self.interval_secs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = f"视频监控循环出错: {e}"
                logger.error(self.last_error)
                await asyncio.sleep(10)

    async def _check_user_videos(self, sub_user: str, record: SubscriptionRecord):
        """
        检查单个用户的视频更新。

        关于置顶作品：抖音用户作品列表会把**置顶作品排在最前面**，而置顶作品往往是旧作。
        因此不能依赖「列表顺序 + 遇到已知 ID 就停」来判新旧：
          - 置顶作品恰好是已知 ID 时, 第一项就 break → 之后的新作品永远检测不到（漏推）
          - 置顶作品换了一个 → 列表首项是未记录过的旧作品 → 会被误当成新作品推送

        现在改为：有 create_time 就以「发布时间 >= 基线时间」判定新作品,
        无 create_time 时退化为「未推送过且非置顶」，置顶作品不再参与顺序推断。
        """
        sec_uid = record.sec_uid or record.uid
        if not sec_uid:
            return

        try:
            await self.amagi.ensure_started()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"ensure_started 异常: {e}")
        if not self._amagi_ready():
            self._log_not_ready()
            return

        try:
            works = await get_user_works(self.amagi, sec_uid)
            if not works:
                logger.debug(f"用户 {sec_uid} 作品列表为空, 跳过")
                return

            items = []   # [(aweme_id, create_time, pinned, raw)]
            for w in works:
                wid = get_aweme_id(w)
                if wid:
                    items.append((wid, get_create_time(w), is_pinned(w), w))
            if not items:
                return

            # 已知 ID：最后推送的 + 最近缓存
            known_ids = set(record.recent_ids or [])
            if record.last_video_id:
                known_ids.add(str(record.last_video_id))
            baseline_time = int(record.last_video_time or 0)

            # 首次订阅：只建立基线, 不推送
            if not record.last_video_id:
                latest = self._pick_baseline(items)
                self._save_video_baseline(sub_user, record, items, latest)
                logger.info(
                    f"首次记录用户 {sec_uid} 的最新视频: {latest[0]} "
                    f"(仅记录基线, 不推送; 之后的更新才会推送)"
                )
                return

            # 旧数据迁移：升级前的老数据没有时间基线 → 只补基线, 不推送, 防止误推置顶/旧作
            if not baseline_time:
                latest = self._pick_baseline(items)
                self._save_video_baseline(sub_user, record, items, latest)
                logger.info("旧数据迁移：已补视频时间基线 (本次不推送)")
                return

            # 收集新作品：未推送过, 且发布时间不早于基线
            new_items = [it for it in items if self._is_new_video(it, known_ids, baseline_time)]
            if not new_items:
                return

            new_items.sort(key=lambda it: it[1] or 0)   # 旧 → 新依次推送
            new_ids = [it[0] for it in new_items]
            newest = new_items[-1]
            nickname = newest[3].get('author', {}).get('nickname', record.nickname)
            logger.info(
                f"检测到用户 {sec_uid} 更新 {len(new_items)} 个新视频: {new_ids} "
                f"(跳过置顶 {sum(1 for it in items if it[2])} 个)"
            )

            # 更新记录：last = 最新ID/时间, recent_ids = 最近一批去重缓存
            updated_recent = list(dict.fromkeys(
                new_ids[::-1] + [str(record.last_video_id)] + (record.recent_ids or [])
            ))[:8]
            self.data_manager.update_subscription(
                sub_user, record.uid, 'video',
                last_video_id=newest[0],
                last_video_time=max(newest[1] or 0, baseline_time),
                recent_ids=updated_recent,
                nickname=nickname
            )

            # 从旧到新推送
            for _wid, _ct, _pinned, work in new_items:
                await self._push_video_message(sub_user, record, work)

        except Exception as e:
            self.last_error = f"检查用户 {sec_uid} 视频失败: {e}"
            logger.error(self.last_error)

    @staticmethod
    def _pick_baseline(items: list) -> tuple:
        """选出基线作品: 取发布时间最新的一条; 都没有时间时退化为首个非置顶作品"""
        timed = [it for it in items if it[1]]
        if timed:
            return max(timed, key=lambda it: it[1])
        normal = [it for it in items if not it[2]]
        return (normal or items)[0]

    @staticmethod
    def _is_new_video(item: tuple, known_ids: set, baseline_time: int) -> bool:
        """判断是否为新发布的作品（置顶旧作不会被判为新）"""
        wid, create_time, pinned, _raw = item
        if wid in known_ids:
            return False
        if create_time:
            return create_time >= baseline_time
        # 拿不到发布时间时保守处理: 只接受非置顶作品
        return not pinned

    def _save_video_baseline(self, sub_user: str, record: SubscriptionRecord,
                             items: list, latest: tuple):
        """写入视频基线 (last_video_id / last_video_time / recent_ids / nickname)"""
        timed = sorted([it for it in items if it[1]], key=lambda it: it[1], reverse=True)
        recent = [it[0] for it in timed[:8]] or [latest[0]]
        self.data_manager.update_subscription(
            sub_user, record.uid, 'video',
            last_video_id=latest[0],
            last_video_time=latest[1] or 0,
            recent_ids=recent,
            nickname=latest[3].get('author', {}).get('nickname', record.nickname),
        )

    # ==================== 渲染结果缓存 ====================

    def _get_cached_render(self, content_id: str) -> Optional[Tuple[str, Optional[str]]]:
        """
        读取渲染结果缓存。

        图片文件可能已被系统清理, 因此命中后仍要确认文件还在, 否则视为未命中。
        """
        if not content_id:
            return None
        cached = self._render_cache.get(content_id)
        if not cached:
            return None
        text, img_path = cached
        if img_path and not Path(img_path).exists():
            self._render_cache.pop(content_id, None)
            return None
        self._render_cache.move_to_end(content_id)
        return cached

    def _cache_render(self, content_id: str, text: str, img_path: Optional[str]) -> None:
        """缓存渲染结果(仅缓存成功渲染出图片的情况)"""
        if not content_id or not img_path:
            return
        self._render_cache[content_id] = (text, img_path)
        self._render_cache.move_to_end(content_id)
        while len(self._render_cache) > self._render_cache_limit:
            self._render_cache.popitem(last=False)

    # ==================== 消息链构建 ====================

    def _resolve_platform_name(self, sub_user: str) -> str:
        """解析会话所属平台适配器的类型名(如 aiocqhttp / telegram)"""
        adapter_id = sub_user.split(":", 1)[0] if ":" in sub_user else ""
        if not adapter_id:
            return ""
        platform_inst = self.context.get_platform_inst(adapter_id)
        if platform_inst:
            return platform_inst.meta().name
        return ""

    @staticmethod
    def _build_text_chain(at_all: bool, text: str, image_url: str = "") -> list:
        """
        构建纯文本消息链 (渲染失败或图片发送失败时的降级方案)。

        image_url 非空时附上一张**平台原图**(抖音封面/头像): 它由协议端自己去下载,
        不经过 t2i 渲染服务, 因此即使渲染链路整体不可用, 推送里依然有图可看。
        """
        parts: list = []
        if at_all:
            parts.append(AtAll())
        parts.append(Plain(text))
        if image_url:
            parts.append(Image.fromURL(image_url))
        return parts

    def _build_image_chain(self, img_path: str, sub_user: str, at_all: bool,
                           caption: str, name_prefix: str) -> list:
        """
        构建图片消息链。

        各平台对图片尺寸/体积有硬限制, 超限的图片会被直接拒收, 因此这里先做
        尺寸自适应: 超限时退化成发送文件(File), 保证内容仍然送得到。
        """
        parts: list = []
        if at_all:
            parts.append(AtAll())

        if is_height_valid(img_path, self._resolve_platform_name(sub_user)):
            parts.append(Image.fromFileSystem(img_path))
        else:
            timestamp = int(time.time())
            parts.append(File(file=img_path, name=f"{name_prefix}_{timestamp}.jpg"))
            logger.info(f"图片超出平台尺寸/体积限制, 改为文件发送: {img_path}")

        parts.append(Plain(f"\n{caption}"))
        return parts

    # ==================== @全体成员权限 ====================

    @staticmethod
    def _extract_group_session(sub_user: str) -> Optional[Tuple[str, str]]:
        """从 UMO 中解析出 (平台ID, 群号); 非群聊返回 None"""
        try:
            platform_id, message_type, session_id = sub_user.split(":", 2)
        except ValueError:
            return None
        if message_type != _GROUP_MESSAGE_TYPE:
            return None
        group_id = session_id.split("_")[-1].strip()
        if not group_id:
            return None
        return platform_id, group_id

    @staticmethod
    def _extract_action_data(action_result) -> dict:
        """兼容 OneBot 接口返回 {data: {...}} 与直接返回 {...} 两种形态"""
        if not isinstance(action_result, dict):
            return {}
        payload = action_result.get("data")
        if isinstance(payload, dict):
            return payload
        return action_result

    async def _check_atall_permission(self, sub_user: str, enabled: bool) -> bool:
        """
        发送 @全体 之前先确认机器人**真的**有权限。

        没有权限时 @全体 会被协议端静默忽略(或整个发送失败), 与其发出去没效果,
        不如提前判定并放弃 @全体 —— 消息本身照常推送。
        """
        if not enabled:
            return False

        group_ctx = self._extract_group_session(sub_user)
        if not group_ctx:
            logger.info(f"@全体仅支持群聊会话, 当前会话: {sub_user}")
            return False

        platform_id, group_id = group_ctx
        platform_inst = self.context.get_platform_inst(platform_id)
        if not platform_inst:
            logger.warning(f"@全体检查失败: 找不到平台实例 {platform_id}")
            return False

        client = platform_inst.get_client()
        if not client or not hasattr(client, "call_action"):
            logger.warning(f"@全体检查失败: 平台 {platform_id} 不支持 call_action")
            return False

        group_id_param: "int | str" = int(group_id) if group_id.isdigit() else group_id

        # 先看机器人自身在群里的角色: 部分实现下 get_group_at_all_remain 并不可靠
        try:
            bot_info = self._extract_action_data(
                await client.call_action("get_login_info")
            )
            bot_id = bot_info.get("user_id")
            if bot_id:
                member_info = self._extract_action_data(
                    await client.call_action(
                        "get_group_member_info",
                        group_id=group_id_param,
                        user_id=bot_id,
                    )
                )
                role = member_info.get("role")
                if role and role not in ("admin", "owner"):
                    logger.info(
                        f"机器人在群 {group_id} 的角色为 {role}, 无 @全体 权限, 本次不 @全体"
                    )
                    return False
        except Exception as e:  # noqa: BLE001
            logger.debug(f"通过 get_group_member_info 检查 @全体 权限失败: {e}")

        try:
            remain_data = self._extract_action_data(
                await client.call_action(
                    "get_group_at_all_remain", group_id=group_id_param
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"调用 get_group_at_all_remain 失败: {e}")
            return False

        if not bool(remain_data.get("can_at_all")):
            logger.info(f"群 {group_id} 当前不允许 @全体成员, 本次不 @全体")
            return False

        group_remain = int(remain_data.get("remain_at_all_count_for_group", 0) or 0)
        self_remain = int(
            remain_data.get(
                "remain_at_all_count_for_self",
                remain_data.get("remain_at_all_count_for_uin", 0),
            )
            or 0
        )
        if group_remain < _MIN_AT_ALL_REMAINING or self_remain < _MIN_AT_ALL_REMAINING:
            logger.info(
                f"群 {group_id} @全体次数不足: group={group_remain}, self={self_remain}"
            )
            return False
        return True

    # ==================== 视频 / 直播推送 ====================

    async def _push_video_message(self, sub_user: str, record: SubscriptionRecord, work: dict):
        """推送视频消息"""
        try:
            nickname = work.get('author', {}).get('nickname', record.nickname or record.uid)
            aweme_id = str(work.get('aweme_id', ''))
            url = build_video_url(aweme_id)
            cover = first_url(work.get('video', {}).get('cover'))

            # 同一视频被多个会话订阅时只渲染一次
            cached = self._get_cached_render(aweme_id)
            if cached:
                logger.debug(f"视频渲染命中缓存: {aweme_id}")
                text, img_path = cached
            else:
                text, img_path = await self.renderer.render_video(work, nickname)
                self._cache_render(aweme_id, text, img_path)

            at_all = await self._check_atall_permission(sub_user, bool(record.at_all))

            if img_path:
                result = await self.dispatcher.publish(SubscriptionNotification(
                    sub_user=sub_user,
                    chain_parts=self._build_image_chain(
                        img_path, sub_user, at_all, url, f"douyin_video_{aweme_id}"
                    ),
                    category="video",
                    content_id=aweme_id,
                ))
                if not result.sent and not result.dropped:
                    # 图片这一步失败(如协议端富媒体上传失败)时, 图与链接在同一条消息里,
                    # 整条推送都会丢, 因此降级重发「纯文本 + 抖音封面原图」
                    logger.warning(f"视频图片推送失败, 降级重发: {result.reason}")
                    await self.dispatcher.publish(SubscriptionNotification(
                        sub_user=sub_user,
                        chain_parts=self._build_text_chain(at_all, text, cover),
                        category="video",
                        content_id=aweme_id,
                    ))
            else:
                await self.dispatcher.publish(SubscriptionNotification(
                    sub_user=sub_user,
                    chain_parts=self._build_text_chain(at_all, text, cover),
                    category="video",
                    content_id=aweme_id,
                ))

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
                self.last_live_scan_at = time.time()
                await asyncio.sleep(self.interval_secs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = f"直播监控循环出错: {e}"
                logger.error(self.last_error)
                await asyncio.sleep(10)

    async def _check_live_status(self, sub_user: str, record: SubscriptionRecord):
        """按订阅用户检查其直播上下播状态"""
        sec_uid = record.sec_uid or ""
        if not sec_uid:
            logger.debug(f"直播订阅缺少 sec_uid, 跳过: {record.uid}")
            return

        try:
            await self.amagi.ensure_started()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"ensure_started 异常: {e}")
        if not self._amagi_ready():
            self._log_not_ready()
            return

        try:
            snap = await get_live_snapshot(self.amagi, sec_uid)
            if not snap:
                return

            # 状态字段缺失(接口偶发不含直播状态) → 视为未知, 既不改状态也不推送,
            # 否则会被误判成「下播」, 造成下播↔开播来回刷屏
            if not snap.get("status_known", True):
                logger.debug(f"直播状态未知, 跳过本轮 (sec_uid={sec_uid})")
                return

            is_now_live = snap["is_live"]
            room_title = snap.get("room_title") or ""

            # 更新昵称
            if snap.get("nickname") and snap["nickname"] != record.nickname:
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    nickname=snap["nickname"],
                )

            # 首次观测只建立基线, 不推送
            # (订阅时主播可能已经开播, 若直接推送会误报成「刚刚开播」)
            if not record.live_checked:
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    live_checked=True,
                    is_live=is_now_live,
                    last_live_title=room_title or record.last_live_title,
                )
                logger.info(
                    f"首次记录用户 {sec_uid} 的直播状态: "
                    f"{'直播中' if is_now_live else '未开播'} "
                    f"(仅记录基线, 不推送; 之后的状态变化才会推送)"
                )
                return

            if is_now_live and not record.is_live:
                # 开播了！
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    is_live=True,
                    last_live_title=room_title,
                )
                logger.info(f"检测到用户 {sec_uid} 开播: {room_title or '无标题'}")
                await self._push_live_message(sub_user, record, True, room_title,
                                              extra={"avatar": snap.get("avatar", "")})

            elif not is_now_live and record.is_live:
                # 下播了
                self.data_manager.update_subscription(
                    sub_user, record.uid, 'live',
                    is_live=False,
                )
                logger.info(f"检测到用户 {sec_uid} 下播")
                await self._push_live_message(sub_user, record, False, record.last_live_title,
                                              extra={"avatar": snap.get("avatar", "")})

        except Exception as e:
            self.last_error = f"检查直播状态失败 (sec_uid={sec_uid}): {e}"
            logger.error(self.last_error)

    async def _push_live_message(self, sub_user: str, record: SubscriptionRecord, is_live: bool,
                                 title: str = "", extra: Optional[dict] = None):
        """推送直播消息"""
        extra = extra or {}

        try:
            avatar = str(extra.get("avatar", "") or "")

            # 使用渲染器生成消息（返回文本 + 可选图片）
            text, img_path = await self.renderer.render_live(
                record, is_live, title,
                avatar=avatar,
            )

            url = build_user_url(record.sec_uid)
            # 只有开播才 @全体 (下播不打扰), 且要先确认机器人真有权限
            at_all = await self._check_atall_permission(
                sub_user, bool(is_live and (record.live_atall or record.at_all))
            )

            if img_path:
                result = await self.dispatcher.publish(SubscriptionNotification(
                    sub_user=sub_user,
                    chain_parts=self._build_image_chain(
                        img_path, sub_user, at_all, url,
                        f"douyin_live_{record.sec_uid}",
                    ),
                    category="live",
                    content_id=str(record.sec_uid or record.uid),
                ))
                if not result.sent and not result.dropped:
                    logger.warning(f"直播图片推送失败, 降级重发: {result.reason}")
                    await self.dispatcher.publish(SubscriptionNotification(
                        sub_user=sub_user,
                        chain_parts=self._build_text_chain(at_all, text, avatar),
                        category="live",
                        content_id=str(record.sec_uid or record.uid),
                    ))
            else:
                await self.dispatcher.publish(SubscriptionNotification(
                    sub_user=sub_user,
                    chain_parts=self._build_text_chain(at_all, text, avatar),
                    category="live",
                    content_id=str(record.sec_uid or record.uid),
                ))

            logger.info(f"已向 {sub_user} 推送直播状态: {'开播' if is_live else '下播'}")
        except Exception as e:
            self.last_error = f"推送直播消息失败: {e}"
            logger.error(self.last_error)
