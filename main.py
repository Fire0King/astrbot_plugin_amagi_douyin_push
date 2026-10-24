import asyncio
import time
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import command, permission_type, PermissionType
from astrbot.core.star.filter.command import GreedyStr

from .core.bilibili import (
    get_live_snapshot as get_bili_live_snapshot,
    get_user_card as get_bili_user_card,
    get_user_videos as get_bili_user_videos,
    parse_mid,
)
from .core.credential_store import CredentialStore
from .core.data_manager import DataManager
from .core.douyin import (
    get_create_time,
    get_live_snapshot,
    get_user_nickname,
    get_user_profile,
    get_user_works,
)
from .core.douyin_login import DouyinLoginError, DouyinQrLoginClient
from .core.models import SubscriptionRecord
from .core.utils import build_user_url, format_number, parse_sec_uid
from .services.amagi_service import AmagiService
from .services.dispatcher import SubscriptionNotificationDispatcher
from .services.listener import DouyinListener
from .services.renderer import Renderer
from .services.subscription_service import SubscriptionService

# 插件根目录
plugin_dir = Path(__file__).parent

# ==================== 重连静默 ====================
# 距上次成功推送超过阈值时, 认为中间积压了更新, 恢复后先静默一段时间
RECONNECT_SILENT_THRESHOLD_SECS = 6 * 3600
# 静默时长 = 一个轮询周期 + 该余量
RECONNECT_SILENT_PADDING_SECS = 60

# ==================== B 站凭据 ====================
# B 站凭据里需要的字段(SESSDATA 必填, 其余用于通过风控)
_BILI_COOKIE_KEYS = ("sessdata", "bili_jct", "buvid3", "dedeuserid", "buvid4", "sid")


def _extract_bili_cookie_from_file(path: Path) -> str:
    """
    从 bilibili 插件的数据文件里提取 B 站凭据, 拼成 Cookie 串。

    只取已知字段, 不打印任何值; 文件不存在/结构不符时返回空串。
    """
    import json as _json

    if not path or not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # 兼容 BOM
            data = _json.load(f)
    except Exception:  # noqa: BLE001
        return ""

    found = []

    def walk(obj):
        if isinstance(obj, dict):
            lower = {str(k).lower(): v for k, v in obj.items()}
            sessdata = lower.get("sessdata")
            if isinstance(sessdata, str) and sessdata.strip():
                pairs = []
                for key in _BILI_COOKIE_KEYS:
                    val = lower.get(key)
                    if isinstance(val, str) and val.strip():
                        name = "SESSDATA" if key == "sessdata" else key
                        pairs.append(f"{name}={val.strip()}")
                if pairs:
                    found.append("; ".join(pairs))
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)

    walk(data)
    return found[0] if found else ""

# ==================== 数据源说明 ====================
# 本插件的数据源为 amagi (https://github.com/ikenxuan/amagi, Node.js SDK):
#   - amagi 不再以 git 子模块分发 (WebUI 安装不会拉子模块, 会导致目录为空),
#     改为插件启动时自动用 npm 安装官方包 @ikenxuan/amagi 到 <插件目录>/.amagi
#   - 插件随后拉起 amagi_bridge/server.mjs (amagi 官方 HTTP 服务)
#   - Python 侧通过 http://127.0.0.1:<amagi_port> 调用 /api/douyin/* 获取数据
# 订阅一律按「抖音用户 (sec_uid/主页URL)」锚定, 直播上下播通过用户主页接口
# 返回的 live_status / live_room 字段判断。


@register(
    "astrbot_plugin_amagi_douyin_push",
    "Fire_King",
    "基于 amagi 的抖音视频更新与直播上下播推送插件",
    "1.4.4",
    "https://github.com/Fire0King/astrbot_plugin_amagi_douyin_push"
)
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.context = context

        # 1. 初始化数据管理器（使用标准数据目录）
        self.data_manager = DataManager()

        # 2. 初始化 amagi 桥接服务
        self.amagi = AmagiService(plugin_dir, self.cfg)
        # 抖音凭据优先级: /dy_login 扫码保存的(plugin_data/credential.json) > 配置里的 cookie
        # (配置在插件升级/WebUI 保存时可能被整体重写, 扫码凭据单独落盘更稳)
        self.credentials = CredentialStore()
        cookie = self._resolve_douyin_cookie()
        self.amagi.set_cookie(cookie)
        # 扫码登录: 后台任务(不占用命令处理流程, 避免被上层取消), 同一时间只允许一个
        self._login_task: Optional[asyncio.Task] = None
        # B 站凭据(附属功能): 可直接粘贴 SESSDATA=...; bili_jct=... 或整段 Cookie;
        # 留空时若开启了 enable_bilibili, 会尝试从 bilibili 插件的数据里借用
        self.enable_bilibili = bool(self.cfg.get("enable_bilibili", False))
        self.amagi.set_bilibili_cookie(self._resolve_bilibili_cookie())

        # 3. 初始化渲染器
        self.rai = self.cfg.get("rai", False)
        self.renderer = Renderer(
            star=self,
            rai=self.rai,
            engine=str(self.cfg.get("card_engine", "local") or "local"),
            font_path=str(self.cfg.get("font_path", "") or ""),
            card_quality=int(self.cfg.get("card_quality", 88) or 88),
            card_scale=float(self.cfg.get("card_scale", 0) or 0),
        )

        # 4. 初始化订阅服务
        self.subscription_service = SubscriptionService(self.data_manager)

        # 5. 初始化通知发送器 (统一出口: 静默模式 / 发送结果 / 成功回调)
        self._last_notify_write_ts = self.data_manager.get_last_success_sub_notify_ts()
        self.dispatcher = SubscriptionNotificationDispatcher(
            context=self.context,
            on_sent=self._on_subscription_notification_sent,
        )

        # 6. 初始化监听服务
        self.listener = DouyinListener(
            context=self.context,
            data_manager=self.data_manager,
            amagi=self.amagi,
            renderer=self.renderer,
            cfg=self.cfg,
            dispatcher=self.dispatcher,
        )
        # 长时间没成功推送过(如断电/断网/长期失败)时先静默一段时间, 避免恢复瞬间刷屏
        self._configure_reconnect_silent()

        # 7. 后台准备并启动 amagi 桥接 (运行时缺失时自动 npm 安装)
        self._amagi_task: Optional[asyncio.Task] = None
        asyncio.create_task(self._boot_amagi())

        # 8. 启动后台监听
        self._listener_task: Optional[asyncio.Task] = None
        self._start_listener()

        if not cookie:
            logger.warning(
                "⚠️ 抖音 Cookie 未配置 —— 请管理员在群里发送 /dy_login 扫码登录, "
                "或在插件配置里填写 douyin_cookie"
            )

    async def _on_subscription_notification_sent(self, _notification) -> None:
        """推送成功回调: 记录时间(同一秒内不重复写盘)"""
        now_ts = int(time.time())
        if now_ts == self._last_notify_write_ts:
            return
        self._last_notify_write_ts = now_ts
        self.data_manager.set_last_success_sub_notify_ts(now_ts)

    def _resolve_bilibili_cookie(self) -> str:
        """
        取得 B 站凭据。

        优先级: 插件配置 bilibili_cookie > 借用 bilibili 插件已保存的凭据(免配置)。
        未开启 enable_bilibili 时直接返回空串(不读别人的数据)。
        """
        explicit = str(self.cfg.get("bilibili_cookie", "") or "").strip()
        if explicit:
            return explicit
        if not self.enable_bilibili:
            return ""

        try:
            from astrbot.api.star import StarTools
            data_dir = Path(StarTools.get_data_dir(
                plugin_name="astrbot_plugin_amagi_douyin_push"))
            candidates = [
                data_dir.parent / "astrbot_plugin_bilibili" / "astrbot_plugin_bilibili.json",
                data_dir.parent.parent / "config" / "astrbot_plugin_bilibili_config.json",
            ]
            for path in candidates:
                cookie = _extract_bili_cookie_from_file(path)
                if cookie:
                    logger.info(
                        f"B站凭据: 已从 bilibili 插件数据借用 ({path.name}), "
                        f"含 {len(cookie.split('; '))} 个字段 (不回显)"
                    )
                    return cookie
        except Exception as e:  # noqa: BLE001
            logger.debug(f"借用 bilibili 插件凭据失败: {e}")

        logger.warning(
            "B站监控已开启, 但未找到可用凭据 —— 请在插件配置里填写 bilibili_cookie "
            "(SESSDATA=...; bili_jct=...; buvid3=...), 或确保 bilibili 插件已登录"
        )
        return ""

    def _resolve_douyin_cookie(self) -> str:
        """
        取得抖音 Cookie。

        优先级: 扫码登录保存的凭据(plugin_data/credential.json)
                > 配置 douyin_cookie > 配置 douyin_live_cookie。

        为什么扫码凭据优先: 配置会被 AstrBot 在升级/保存设置时整体重写,
        2026-10-20 就发生过 douyin_cookie 被清空导致桥接不启动。
        """
        stored = ""
        try:
            stored = self.credentials.get_cookie()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"读取扫码凭据失败: {e}")
        if stored:
            nickname = self.credentials.nickname() or "未知"
            source = self.credentials.source() or "qr_login"
            logger.info(
                f"抖音 Cookie: 使用已保存的凭据 (来源={source}, 昵称={nickname}, "
                f"长度={len(stored)}, 不回显)"
            )
            return stored
        return (self.cfg.get("douyin_cookie", "").strip()
                or self.cfg.get("douyin_live_cookie", "").strip())

    def _configure_reconnect_silent(self) -> None:
        """
        重连静默: 若距上次成功推送已超过阈值(默认 6 小时), 认为中间积压了大量更新,
        先静默一个轮询周期 + 60 秒, 避免一次性把积压内容全部推出去刷屏。
        """
        if not bool(self.cfg.get("reconnect_silent", False)):
            self.dispatcher.set_silent_until_ts(0)
            return

        last_success_ts = self.data_manager.get_last_success_sub_notify_ts()
        if last_success_ts <= 0:
            logger.info("重连静默未触发: 缺少历史推送成功时间。")
            return

        now_ts = int(time.time())
        idle_secs = now_ts - last_success_ts
        if idle_secs <= RECONNECT_SILENT_THRESHOLD_SECS:
            logger.info(
                f"重连静默未触发: 距上次成功推送仅 {idle_secs} 秒 "
                f"(阈值 {RECONNECT_SILENT_THRESHOLD_SECS} 秒)。"
            )
            return

        silent_secs = max(10, int(self.cfg.get("poll_interval", 60))) + RECONNECT_SILENT_PADDING_SECS
        self.dispatcher.set_silent_until_ts(now_ts + silent_secs)
        logger.warning(
            f"检测到长时间未成功推送({idle_secs} 秒), 进入静默模式 {silent_secs} 秒。"
        )

    # ==================== amagi 桥接 ====================

    async def _boot_amagi(self):
        """准备 amagi 运行时并拉起常驻桥接进程"""
        try:
            await self.amagi.prepare()
            await self.amagi.ensure_started()
        except Exception as e:
            logger.error(f"启动 amagi 桥接任务异常: {e}")

    # ==================== 监听任务管理 ====================

    def _start_listener(self):
        """启动后台监听"""
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = asyncio.create_task(self.listener.start())
        logger.info("后台监听任务已启动")

    async def _restart_listener(self):
        """
        重启监听服务。

        必须 await 旧任务真正结束: 只 cancel() 而不等待时, 旧任务尚未处理完
        CancelledError, 新任务可能被误判为「已在运行」而直接返回, 导致监听静默失效。
        """
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug(f"旧监听任务结束异常: {e}")
        self._listener_task = asyncio.create_task(self.listener.start())
        logger.info("监听服务已重启")

    # ==================== 用户命令 ====================

    @command("dy_sub")
    async def dy_sub(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        订阅抖音用户的视频更新和直播状态。

        用法:
          /dy_sub <URL/sec_uid> [at_all|live_atall]  — 订阅视频+直播
          /dy_sub video <URL/sec_uid> [at_all]       — 仅订阅视频
          /dy_sub live <URL/sec_uid> [live_atall]    — 仅订阅直播

        选项说明:
          at_all      — 发视频/开播时 @全体成员（仅管理员）
          live_atall  — 仅开播时 @全体成员（仅管理员）

        示例:
          /dy_sub https://www.douyin.com/user/MS4wLjABAAAA... at_all
          /dy_sub video MS4wLjABAAAA...
          /dy_sub live MS4wLjABAAAA... live_atall
        """
        sub_user = event.unified_msg_origin
        args = raw_args.strip().split() if raw_args.strip() else []

        if not args:
            yield event.plain_result(
                "❌ 请提供订阅参数。\n"
                "用法:\n"
                "  /dy_sub <URL/sec_uid> [at_all|live_atall]  # 视频+直播\n"
                "  /dy_sub video <URL/sec_uid> [at_all]       # 仅视频\n"
                "  /dy_sub live <URL/sec_uid> [live_atall]    # 仅直播\n"
                "选项: at_all=全部@全体, live_atall=仅开播@全体"
            )
            return

        # 解析 @全体 选项
        at_all = False
        live_atall = False
        options = {'at_all', 'live_atall'}
        filtered_args = [a for a in args if a not in options]
        for a in args:
            if a == 'at_all':
                at_all = True
            elif a == 'live_atall':
                live_atall = True

        # 权限检查：只有管理员可以设置 @全体
        if (at_all or live_atall) and not event.is_admin():
            yield event.plain_result("❌ 权限不足：只有管理员可以设置 @全体成员 相关选项。")
            return

        # 解析 sub_type 和 target
        sub_type = 'both'
        target = filtered_args[0]

        if target in ('video', 'live', 'both') and len(filtered_args) > 1:
            sub_type = target
            target = filtered_args[1]

        # 直播订阅为「用户」语义, 只接受 sec_uid / 用户主页 URL
        sec_uid = parse_sec_uid(target)
        if not sec_uid:
            if sub_type == 'live':
                yield event.plain_result(
                    "❌ 未识别到有效的抖音用户标识。\n"
                    "直播监控按「主播用户」轮询:\n"
                    "  /dy_sub live <主播主页URL或sec_uid> [live_atall]\n"
                    "请提供主播主页 URL 或 sec_uid（不支持直播间房间号）。"
                )
            else:
                yield event.plain_result("❌ 未识别到有效的抖音用户URL或sec_uid")
            return

        results = []
        nickname = await self._fetch_user_nickname(sec_uid)
        nickname = nickname or sec_uid

        # 视频部分
        if sub_type in ('video', 'both'):
            success, msg = await self.subscription_service.add_subscription(
                sub_user, sec_uid, 'video',
                sec_uid=sec_uid,
                nickname=nickname,
                at_all=at_all,
            )
            results.append(msg)

        # 直播部分
        if sub_type in ('live', 'both'):
            success, msg = await self.subscription_service.add_subscription(
                sub_user, sec_uid, 'live',
                sec_uid=sec_uid,
                nickname=nickname,
                at_all=at_all,
                live_atall=live_atall,
            )
            results.append(msg)

        await self._restart_listener()
        yield event.plain_result("\n".join(results))

    async def _fetch_user_nickname(self, sec_uid: str) -> Optional[str]:
        """从 amagi 用户主页接口获取用户昵称"""
        if not self.amagi.cookie_configured:
            return None
        try:
            return await get_user_nickname(self.amagi, sec_uid)
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")
        return None

    @command("dy_unsub")
    async def dy_unsub(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        取消订阅。

        用法: /dy_unsub <sec_uid/直播间ID> [video/live]
        """
        sub_user = event.unified_msg_origin
        args = raw_args.strip().split(None, 1) if raw_args.strip() else []

        if not args:
            yield event.plain_result("❌ 请提供要取消订阅的ID。\n用法: /dy_unsub <sec_uid/直播间ID> [video/live]")
            return

        uid = args[0]
        sub_type = args[1] if len(args) > 1 else None

        if sub_type:
            success, msg = await self.subscription_service.remove_subscription(
                sub_user, uid, sub_type
            )
            if success:
                await self._restart_listener()
            yield event.plain_result(msg)
        else:
            # 尝试移除 video 和 live
            results = []
            for st in ['video', 'live']:
                success, msg = await self.subscription_service.remove_subscription(
                    sub_user, uid, st
                )
                if success:
                    results.append(msg)
            if results:
                await self._restart_listener()
                yield event.plain_result("\n".join(results))
            else:
                yield event.plain_result(f"⚠️ 未找到 {uid} 的订阅")

    @command("dy_sub_list", alias={"订阅列表"})
    async def dy_sub_list(self, event: AstrMessageEvent):
        """列出当前会话的所有订阅"""
        sub_user = event.unified_msg_origin
        records = await self.subscription_service.list_subscriptions(sub_user)

        if not records:
            yield event.plain_result("📋 当前没有订阅")
            return

        msg_parts = ["📋 当前订阅列表\n"]
        for i, r in enumerate(records, 1):
            type_tag = "📹视频" if r.sub_type == 'video' else "🔴直播"
            status = " 🟢直播中" if r.is_live else ""
            at_tag = ""
            if r.at_all:
                at_tag = " [@全体]"
            elif r.live_atall:
                at_tag = " [开播@全体]"
            nickname = r.nickname or r.uid
            msg_parts.append(f"{i}. {type_tag} {nickname}{at_tag}{status}")

        yield event.plain_result("\n".join(msg_parts))

    # ==================== B 站命令 (附属功能) ====================

    def _bili_unavailable_msg(self) -> str:
        """B 站功能不可用时的提示 (未开启 / 缺凭据)"""
        if not self.enable_bilibili:
            return (
                "❌ B站监控未开启。\n"
                "请在插件配置里打开 enable_bilibili 并准备好 B 站凭据"
                "(填 bilibili_cookie, 或让 bilibili 插件保持登录以便自动借用), 然后重载插件。"
            )
        if not self.amagi.bilibili_cookie_configured:
            return (
                "❌ B站凭据缺失: 请在插件配置里填写 bilibili_cookie "
                "(形如 SESSDATA=xxx; bili_jct=xxx; buvid3=xxx), 或让 bilibili 插件保持登录。"
            )
        return ""

    @command("bili_sub")
    async def bili_sub(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        订阅 B 站 UP 主的新投稿与开播/下播（附属功能）。

        用法:
          /bili_sub <UID|主页URL> [at_all|live_atall]  — 投稿+直播
          /bili_sub video <UID|主页URL> [at_all]       — 仅投稿
          /bili_sub live <UID|主页URL> [live_atall]    — 仅直播
        """
        sub_user = event.unified_msg_origin
        warn = self._bili_unavailable_msg()
        if warn:
            yield event.plain_result(warn)
            return

        args = raw_args.strip().split() if raw_args.strip() else []
        if not args:
            yield event.plain_result(
                "❌ 请提供 B 站 UID 或主页链接。\n"
                "用法:\n"
                "  /bili_sub <UID|主页URL> [at_all|live_atall]  # 投稿+直播\n"
                "  /bili_sub video <UID|主页URL> [at_all]       # 仅投稿\n"
                "  /bili_sub live <UID|主页URL> [live_atall]    # 仅直播\n"
                "示例: /bili_sub https://space.bilibili.com/525972018 at_all"
            )
            return

        at_all = 'at_all' in args
        live_atall = 'live_atall' in args
        if (at_all or live_atall) and not event.is_admin():
            yield event.plain_result("❌ 权限不足：只有管理员可以设置 @全体成员 相关选项。")
            return

        filtered = [a for a in args if a not in ('at_all', 'live_atall')]
        sub_type = 'both'
        target = filtered[0]
        if target in ('video', 'live', 'both') and len(filtered) > 1:
            sub_type = target
            target = filtered[1]

        mid = parse_mid(target)
        if not mid:
            yield event.plain_result(
                "❌ 未识别到有效的 B 站 UID。\n"
                "请提供数字 UID（如 525972018）或主页链接（https://space.bilibili.com/525972018）"
            )
            return

        # 取昵称(顺带验证凭据是否可用)
        nickname = mid
        try:
            card = await get_bili_user_card(self.amagi, mid)
            if card:
                nickname = str(card.get("name") or mid)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"获取 B 站用户名片失败: {e}")

        results = []
        if sub_type in ('video', 'both'):
            _, msg = await self.subscription_service.add_subscription(
                sub_user, mid, 'video',
                nickname=nickname,
                at_all=at_all,
                platform='bilibili',
            )
            results.append(msg)

        if sub_type in ('live', 'both'):
            room_id = ""
            try:
                snap = await get_bili_live_snapshot(self.amagi, mid)
                room_id = str(snap.get("room_id") or "") if snap else ""
            except Exception as e:  # noqa: BLE001
                logger.debug(f"获取 B 站直播间信息失败: {e}")
            _, msg = await self.subscription_service.add_subscription(
                sub_user, mid, 'live',
                nickname=nickname,
                room_id=room_id,
                at_all=at_all,
                live_atall=live_atall,
                platform='bilibili',
            )
            results.append(msg)

        await self._restart_listener()
        yield event.plain_result("\n".join(results))

    @command("bili_unsub")
    async def bili_unsub(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """取消 B 站订阅。用法: /bili_unsub <UID> [video/live]"""
        sub_user = event.unified_msg_origin
        args = raw_args.strip().split(None, 1) if raw_args.strip() else []
        if not args:
            yield event.plain_result("❌ 请提供要取消订阅的 UID。\n用法: /bili_unsub <UID> [video/live]")
            return

        uid = parse_mid(args[0]) or args[0]
        sub_type = args[1] if len(args) > 1 else None

        if sub_type:
            success, msg = await self.subscription_service.remove_subscription(
                sub_user, uid, sub_type, 'bilibili')
            if success:
                await self._restart_listener()
            yield event.plain_result(msg)
            return

        results = []
        removed = False
        for st in ('video', 'live'):
            success, msg = await self.subscription_service.remove_subscription(
                sub_user, uid, st, 'bilibili')
            results.append(msg)
            removed = removed or success
        if removed:
            await self._restart_listener()
        yield event.plain_result("\n".join(results))

    @command("bili_list", alias={"B站订阅列表"})
    async def bili_list(self, event: AstrMessageEvent):
        """列出当前会话的 B 站订阅"""
        sub_user = event.unified_msg_origin
        records = await self.subscription_service.list_subscriptions(sub_user, 'bilibili')
        if not records:
            yield event.plain_result("📋 当前没有 B 站订阅")
            return

        msg_parts = ["📋 B站订阅列表\n"]
        for i, r in enumerate(records, 1):
            type_tag = "📺投稿" if r.sub_type == 'video' else "🔴直播"
            status = " 🟢直播中" if r.is_live else ""
            at_tag = " [@全体]" if r.at_all else (" [开播@全体]" if r.live_atall else "")
            msg_parts.append(f"{i}. {type_tag} {r.nickname or r.uid}{at_tag}{status}")
        yield event.plain_result("\n".join(msg_parts))

    @command("bili_info")
    async def bili_info(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """查询 B 站 UP 主信息。用法: /bili_info <UID|主页URL>"""
        warn = self._bili_unavailable_msg()
        if warn:
            yield event.plain_result(warn)
            return
        mid = parse_mid(raw_args.strip())
        if not mid:
            yield event.plain_result("❌ 请提供 B 站 UID 或主页链接。用法: /bili_info 525972018")
            return
        try:
            card = await get_bili_user_card(self.amagi, mid)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"❌ 查询失败: {e}")
            return
        if not card:
            yield event.plain_result("❌ 未获取到该 UP 主信息（UID 是否正确 / 凭据是否有效）")
            return
        yield event.plain_result(self.renderer.render_bili_user_info(card))

    @command("dy_test")
    async def dy_test(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        测试订阅功能。获取指定用户的最新视频/直播状态并推送测试消息，不保存订阅信息。

        用法:
          /dy_test <sec_uid>           — 测试视频推送
          /dy_test live <sec_uid>      — 测试直播状态
        """
        args = raw_args.strip().split(None, 1) if raw_args.strip() else []
        if not args:
            yield event.plain_result("❌ 请提供 sec_uid 或用户主页URL。\n用法: /dy_test <sec_uid> 或 /dy_test live <sec_uid>")
            return

        sub_user = event.unified_msg_origin
        is_live_test = (args[0] == 'live' and len(args) > 1)
        target = args[1] if is_live_test else args[0]

        sec_uid = parse_sec_uid(target)
        if not sec_uid:
            yield event.plain_result("❌ 无法识别 sec_uid，请提供抖音用户URL或sec_uid")
            return

        if not self.amagi.cookie_configured:
            yield event.plain_result("❌ 抖音 Cookie 未配置，无法测试")
            return

        try:
            await self.amagi.ensure_started()
        except Exception as e:
            logger.debug(f"amagi 桥接启动失败: {e}")

        if not (self.amagi.running and self.amagi.started):
            yield event.plain_result(
                f"❌ amagi 桥接未就绪, 无法测试。\n"
                f"状态: {self.amagi.status_info().get('build_msg', '未知')}\n"
                f"错误: {self.amagi.status_info().get('last_error') or '无'}"
            )
            return

        try:
            if is_live_test:
                # 测试直播状态 (按用户)
                yield event.plain_result(f"⏳ 正在查询用户 {sec_uid} 的直播状态...")
                snap = await get_live_snapshot(self.amagi, sec_uid)

                if not snap:
                    yield event.plain_result("❌ 获取直播信息失败，请检查 Cookie 是否有效")
                    return

                is_live = snap["is_live"]
                status_text = "🟢 直播中" if is_live else "⭕ 未开播"
                nickname = snap["nickname"] or sec_uid

                dummy_record = SubscriptionRecord(
                    sub_user=sub_user, uid=sec_uid,
                    sub_type='live', sec_uid=sec_uid, nickname=nickname
                )
                await self.listener._push_live_message(
                    sub_user, dummy_record,
                    is_live=is_live,
                    title=snap.get("room_title") or "无标题",
                    extra={"avatar": snap.get("avatar", "")},
                )
                if snap.get("status_known"):
                    detail = f"\n判定依据: {snap.get('status_source')}={snap.get('room_status')}"
                else:
                    detail = "\n⚠️ 未读到直播状态字段(live_room.status / user.live_status 均缺失)，无法判定"
                yield event.plain_result(
                    f"✅ 直播状态: {status_text}{detail}\n"
                    f"👤 {nickname}\n测试消息已发送到当前会话"
                )

            else:
                # 测试视频
                yield event.plain_result(f"⏳ 正在查询用户 {sec_uid} 最新视频...")
                works = await get_user_works(self.amagi, sec_uid)

                if not works:
                    yield event.plain_result("❌ 未获取到视频数据，请检查 Cookie 或 sec_uid 是否正确")
                    return

                # 列表首位可能是「置顶旧作」, 因此按发布时间取真正最新的一条
                candidates = [w for w in works if get_create_time(w)]
                latest = max(candidates, key=get_create_time) if candidates else works[0]
                nickname = latest.get('author', {}).get('nickname', sec_uid)

                dummy_record = SubscriptionRecord(
                    sub_user=sub_user, uid=sec_uid,
                    sub_type='video', sec_uid=sec_uid, nickname=nickname
                )
                await self.listener._push_video_message(sub_user, dummy_record, latest)

                desc = latest.get('desc', '无标题')[:50]
                yield event.plain_result(
                    f"✅ 已获取到 {nickname} 的最新视频\n"
                    f"📝 {desc}\n"
                    f"测试消息已发送到当前会话"
                )

        except Exception as e:
            logger.error(f"测试失败: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @command("bili_test")
    async def bili_test(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        测试 B 站订阅功能。获取指定 UP 主的最新投稿/直播状态并推送测试消息，不保存订阅信息。

        用法:
          /bili_test <UID|主页URL>         — 测试投稿推送
          /bili_test live <UID|主页URL>    — 测试直播状态
        """
        args = raw_args.strip().split(None, 1) if raw_args.strip() else []
        if not args:
            yield event.plain_result(
                "❌ 请提供 B 站 UID 或主页URL。\n"
                "用法: /bili_test <UID> 或 /bili_test live <UID>"
            )
            return

        warn = self._bili_unavailable_msg()
        if warn:
            yield event.plain_result(warn)
            return

        sub_user = event.unified_msg_origin
        is_live_test = (args[0] == 'live' and len(args) > 1)
        target = args[1] if is_live_test else args[0]

        mid = parse_mid(target)
        if not mid:
            yield event.plain_result("❌ 无法识别 UID，请提供 B 站主页链接或纯数字 UID")
            return

        try:
            await self.amagi.ensure_started()
        except Exception as e:
            logger.debug(f"amagi 桥接启动失败: {e}")

        if not (self.amagi.running and self.amagi.started):
            status = self.amagi.status_info()
            yield event.plain_result(
                f"❌ amagi 桥接未就绪, 无法测试。\n"
                f"状态: {status.get('build_msg', '未知')}\n"
                f"错误: {status.get('last_error') or '无'}"
            )
            return

        try:
            # 昵称/头像取自用户名片(直播接口不带这些字段), 取不到就退化成 UID
            nickname = mid
            avatar = ""
            try:
                card = await get_bili_user_card(self.amagi, mid)
                if card:
                    nickname = str(card.get("name") or mid)
                    avatar = str(card.get("face") or "")
            except Exception as e:
                logger.debug(f"获取B站用户名片失败: {e}")

            if is_live_test:
                yield event.plain_result(f"⏳ 正在查询 UP 主 {nickname} 的直播状态...")
                snap = await get_bili_live_snapshot(self.amagi, mid)
                if not snap:
                    yield event.plain_result("❌ 获取直播信息失败，请检查 B 站凭据是否有效")
                    return

                is_live = bool(snap.get("is_live"))
                record = SubscriptionRecord(
                    sub_user=sub_user, uid=mid, sub_type='live', platform='bilibili',
                    nickname=nickname, room_id=str(snap.get("room_id") or ""),
                )
                await self.listener._push_bili_live_message(
                    sub_user, record, is_live,
                    snap.get("room_title") or "无标题",
                    cover=snap.get("cover", ""),
                )
                if snap.get("status_known"):
                    detail = f"\n判定依据: {snap.get('status_source')} (状态已知)"
                else:
                    detail = "\n⚠️ 未读到直播状态字段, 无法判定开播/下播"
                yield event.plain_result(
                    f"✅ 直播状态: {'🟢 直播中' if is_live else '⭕ 未开播'}{detail}\n"
                    f"👤 {nickname} (UID {mid})\n测试消息已发送到当前会话"
                )

            else:
                yield event.plain_result(f"⏳ 正在查询 UP 主 {nickname} 的最新投稿...")
                videos = await get_bili_user_videos(self.amagi, mid)
                if not videos:
                    yield event.plain_result("❌ 未获取到投稿数据，请检查 B 站凭据或 UID 是否正确")
                    return

                # 列表首位可能是「置顶旧作」, 因此按发布时间取真正最新的一条
                candidates = [v for v in videos if v.get("pub_ts")]
                latest = max(candidates, key=lambda v: v.get("pub_ts") or 0) if candidates else videos[0]

                record = SubscriptionRecord(
                    sub_user=sub_user, uid=mid, sub_type='video', platform='bilibili',
                    nickname=nickname,
                )
                await self.listener._push_bili_video_message(sub_user, record, latest)

                yield event.plain_result(
                    f"✅ 已获取到 {nickname} 的最新投稿\n"
                    f"📝 {str(latest.get('title') or '无标题')[:50]}\n"
                    f"🔗 {latest.get('url') or ''}\n"
                    f"测试消息已发送到当前会话"
                )

        except Exception as e:
            logger.error(f"B站测试失败: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @command("dy_clear")
    @permission_type(PermissionType.ADMIN)
    async def dy_clear(self, event: AstrMessageEvent):
        """清空当前会话的所有订阅（管理员）"""
        sub_user = event.unified_msg_origin
        msg = await self.subscription_service.remove_all_for_user(sub_user)
        await self._restart_listener()
        yield event.plain_result(msg)

    @command("dy_info")
    async def dy_info(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        获取抖音用户信息。

        用法: /dy_info <抖音用户URL或sec_uid>
        """
        target = raw_args.strip()
        if not target:
            yield event.plain_result("❌ 请提供抖音用户URL或sec_uid")
            return

        sec_uid = parse_sec_uid(target)
        if not sec_uid:
            yield event.plain_result("❌ 无法识别，请提供有效的抖音用户URL或sec_uid")
            return

        if not self.amagi.cookie_configured:
            yield event.plain_result("❌ 抖音 Cookie 未配置，无法查询用户信息")
            return

        try:
            profile = await get_user_profile(self.amagi, sec_uid)

            if not profile or 'user' not in profile:
                yield event.plain_result("❌ 获取用户信息失败，请检查 Cookie 是否有效")
                return

            user = profile['user']
            nickname = user.get('nickname', '未知')
            signature = user.get('signature', '这个人很懒，什么都没写')
            follower = format_number(user.get('follower_count', 0))
            following = format_number(user.get('following_count', 0))
            total_favorited = format_number(user.get('total_favorited', 0))
            aweme_count = user.get('aweme_count', 0)
            user_url = build_user_url(sec_uid)

            msg = (
                f"👤 {nickname}\n"
                f"📝 {signature[:100]}\n"
                f"👥 粉丝: {follower}  ·  关注: {following}\n"
                f"❤️ 获赞: {total_favorited}  ·  作品: {aweme_count}\n"
                f"🔗 {user_url}"
            )
            yield event.plain_result(msg)

        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")
            yield event.plain_result(f"❌ 获取用户信息失败: {str(e)}")

    # ==================== 管理员命令 ====================

    @command("dy_global_list")
    @permission_type(PermissionType.ADMIN)
    async def dy_global_list(self, event: AstrMessageEvent):
        """查看所有会话的订阅（管理员）"""
        all_subs = self.data_manager.get_all_subscriptions()
        if not all_subs or not any(all_subs.values()):
            yield event.plain_result("📋 暂无任何订阅")
            return

        msg_parts = ["📋 全局订阅列表"]
        total = 0
        for sub_user, records in all_subs.items():
            if records:
                msg_parts.append(f"\n📌 {sub_user}:")
                for r in records:
                    total += 1
                    tag = "📹" if r.sub_type == 'video' else "🔴"
                    name = r.nickname or r.uid
                    status = " 🟢" if r.is_live else ""
                    msg_parts.append(f"  {tag} {name} ({r.sub_type}){status}")
        msg_parts.append(f"\n总计: {total} 个订阅")
        yield event.plain_result("".join(msg_parts))

    @command("dy_global_unsub")
    @permission_type(PermissionType.ADMIN)
    async def dy_global_unsub(self, event: AstrMessageEvent, raw_args: GreedyStr):
        """
        删除指定会话指定用户的订阅（管理员）。

        用法: /dy_global_unsub <会话UMO> <UID> [bilibili]
        末尾加 bilibili 时删的是 B 站订阅(默认删抖音)。
        """
        args = raw_args.strip().split() if raw_args.strip() else []
        if len(args) < 2:
            yield event.plain_result("❌ 用法: /dy_global_unsub <会话UMO> <UID> [bilibili]")
            return
        target_user = args[0]
        target_uid = parse_mid(args[1]) if args[1].isdigit() else args[1]
        platform = "bilibili" if any(a.lower() == "bilibili" for a in args[2:]) else "douyin"
        for st in ['video', 'live']:
            self.data_manager.remove_subscription(target_user, target_uid, st, platform)
        await self._restart_listener()
        tag = "B站" if platform == "bilibili" else "抖音"
        yield event.plain_result(f"✅ 已移除 {target_user} 的{tag}订阅 {target_uid}")

    @command("dy_bridge_restart")
    @permission_type(PermissionType.ADMIN)
    async def dy_bridge_restart(self, event: AstrMessageEvent):
        """重启 amagi 数据桥接（管理员；改 Cookie 后调用，也会重新检查/安装 amagi 运行时）"""
        # 重新读取凭据 (扫码保存的 / 配置里的; 兼容 AstrBot 运行期改配置)
        cookie = self._resolve_douyin_cookie()
        self.amagi.set_cookie(cookie)
        # B 站凭据(附属功能): 配置里的优先, 否则重新尝试借用 bilibili 插件的凭据
        self.enable_bilibili = bool(self.cfg.get("enable_bilibili", False))
        self.amagi.set_bilibili_cookie(self._resolve_bilibili_cookie())
        try:
            await self.amagi.prepare()
        except Exception as e:
            yield event.plain_result(f"❌ amagi 构建检查失败: {e}")
            return
        try:
            ok = await self.amagi.restart()
        except Exception as e:
            ok = False
            logger.error(f"重启 amagi 桥接异常: {e}")
        if ok:
            yield event.plain_result(
                f"✅ amagi 桥接已重启 (http://{self.amagi.host}:{self.amagi.port}, "
                f"cookie={'已配置' if self.amagi.cookie_configured else '未配置'}, "
                f"B站凭据={'已配置' if self.amagi.bilibili_cookie_configured else '未配置'})"
            )
        else:
            yield event.plain_result(
                f"❌ amagi 桥接重启失败。\n"
                f"状态: {self.amagi.status_info().get('build_msg', '')}\n"
                f"错误: {self.amagi.status_info().get('last_error') or '无'}"
            )

    @command("dy_login")
    @permission_type(PermissionType.ADMIN)
    async def dy_login(self, event: AstrMessageEvent):
        """
        扫码登录抖音（管理员）：后台出码并把二维码发到当前会话，扫完自动保存凭据并重启桥接。

        用法: /dy_login
        """
        if self._login_busy():
            yield event.plain_result(
                "⚠️ 已有一个扫码登录在进行中，请先把那张二维码扫掉，或等它结束（最长 10 分钟）再试。"
            )
            return

        timeout_secs = 300
        try:
            timeout_secs = int(self.cfg.get("login_timeout", 300) or 300)
        except (TypeError, ValueError):
            timeout_secs = 300
        timeout_secs = max(60, min(600, timeout_secs))

        umo = event.unified_msg_origin
        # 关键: 登录过程放到后台任务里跑。命令处理流程可能被上层提前结束/取消,
        # 而扫码需要用户 3~5 分钟, 不能依赖它活着。
        self._login_task = asyncio.create_task(self._run_dy_login(umo, timeout_secs))
        yield event.plain_result(
            f"⏳ 已开始扫码登录（后台执行，窗口 {timeout_secs} 秒）。\n"
            f"二维码生成后会直接发到本会话（约 20~60 秒）。\n"
            f"⚠️ 抖音的码**约 1 分钟自动换一次**，收到就立刻扫；换码时我会自动补发最新的一张，以最后一张为准。"
        )

    def _login_busy(self) -> bool:
        return bool(self._login_task and not self._login_task.done())

    async def _run_dy_login(self, umo: str, timeout_secs: int) -> None:
        """后台执行扫码登录（不占用命令处理流程）。

        抖音的登录二维码约 40 秒就自动换一张，我们发出去的是截图，不补发的话
        用户扫到时大概率已经作废（这是"扫了没反应、最后还是过期"的主因）。
        所以等待期间: 每 ~4 秒比一次截图, 码换了就补发最新的一张;
        另外每 2 秒读一次页面文字, 扫到就立刻提示"请在手机上点确认"。
        最后: 成功则落盘凭据 + 重启桥接 + 真机验证; 超时/异常都明确回一条消息。
        """
        client = DouyinQrLoginClient(timeout_secs=timeout_secs)
        max_resends = 10
        try:
            qr = await client.start()
            await self._send_to(umo, MessageChain()
                                .message(
                                    f"📱 请用**抖音 App** 扫描下方二维码登录。\n"
                                    f"⚠️ 抖音的码**约 1 分钟自动换一次**，收到就立刻扫；"
                                    f"换了我会再发新的一张，以最后一张为准。\n"
                                    f"扫到后手机上点「确认登录」，成功后自动保存凭据并重启桥接。"
                                )
                                .file_image(qr.image_path))
            logger.info(f"扫码登录: 二维码已发送, 开始等待扫码 (超时 {timeout_secs}s)")

            deadline = time.time() + timeout_secs
            scanned_notified = False
            errors = 0
            ticks = 0
            resends = 0
            while time.time() < deadline:
                result = await client.poll()
                if result.is_done:
                    await self._finish_dy_login(umo, result.cookie)
                    return
                if result.state == "error":
                    errors += 1
                    logger.warning(f"扫码登录轮询异常 (第 {errors} 次): {result.message}")
                    if errors >= 5:
                        await self._send_to(umo, MessageChain().message(
                            "❌ 登录会话中断（浏览器异常），请重新发送 /dy_login。"
                        ))
                        return
                else:
                    errors = 0

                state = await client.page_state()
                if state == "scanned" and not scanned_notified:
                    scanned_notified = True
                    logger.info("扫码登录: 已扫码, 等待手机确认")
                    await self._send_to(umo, MessageChain().message(
                        "📱 已扫码，请在手机上点「确认登录」。"
                    ))

                # 每两轮(~4s)检查一次二维码是否轮换, 变了就补发（以最新一张为准）
                if ticks % 2 == 0 and resends < max_resends:
                    new_path = await client.refresh_qr()
                    if new_path:
                        resends += 1
                        scanned_notified = False
                        logger.info(f"扫码登录: 二维码已轮换, 补发第 {resends} 张")
                        await self._send_to(umo, MessageChain()
                                            .message("♻️ 抖音的二维码自动换了一张，请扫这张最新的（旧码已作废）：")
                                            .file_image(new_path))
                ticks += 1
                await asyncio.sleep(2)

            await self._send_to(umo, MessageChain().message(
                "⌛ 二维码已过期（没人扫或没在手机上点确认）。\n需要的话再发一次 /dy_login。"
            ))
        except asyncio.CancelledError:
            logger.info("扫码登录任务被取消（插件卸载/重载）")
            raise
        except DouyinLoginError as e:
            logger.error(f"扫码登录失败: {e}")
            await self._send_to(umo, MessageChain().message(f"❌ 扫码登录失败: {e}"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"扫码登录异常: {e}")
            await self._send_to(umo, MessageChain().message(f"❌ 扫码登录异常: {e}"))
        finally:
            # 被取消时也要尽量把浏览器收掉（shield 保证清理协程不会被同一次取消打断）
            try:
                await asyncio.shield(client.close())
            except Exception:  # noqa: BLE001
                pass

    async def _send_to(self, umo: str, chain) -> bool:
        """主动给指定会话发消息（后台任务用；不依赖 event 的生命周期）。"""
        try:
            ok = await self.context.send_message(umo, chain)
            if ok is False:
                logger.warning("主动发送消息返回 False（未找到匹配的平台?）")
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            logger.error(f"主动发送消息失败: {e}")
            return False

    async def _finish_dy_login(self, umo: str, cookie: str) -> None:
        """扫码成功后: 落盘凭据 → 重启桥接 → 真机验证一次 → 汇报。"""
        try:
            self.credentials.save_cookie(cookie, source="qr_login")
        except Exception as e:  # noqa: BLE001
            logger.error(f"保存扫码凭据失败: {e}")
            await self._send_to(umo, MessageChain().message(f"⚠️ 扫码成功，但保存凭据失败: {e}"))
            return

        self.amagi.set_cookie(cookie)
        bridge_ok = False
        try:
            bridge_ok = await self.amagi.restart()
        except Exception as e:  # noqa: BLE001
            logger.error(f"扫码后重启桥接失败: {e}")

        # 真机验证: 用任一抖音订阅的 sec_uid 探一次, 顺便把昵称补进凭据文件
        nickname, verified = "", ""
        try:
            sec_uid = self._first_douyin_sec_uid()
            if sec_uid:
                profile = await get_user_profile(self.amagi, sec_uid)
                if profile:
                    nickname = str((profile.get("user") or {}).get("nickname") or "")
                    if nickname:
                        try:
                            self.credentials.save_cookie(cookie, source="qr_login", nickname=nickname)
                        except Exception:  # noqa: BLE001
                            pass
                    works = await get_user_works(self.amagi, sec_uid)
                    verified = f"取到 {len(works or [])} 条作品"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"扫码后验证接口失败: {e}")

        lines = ["✅ 抖音扫码登录成功！"]
        if nickname:
            lines.append(f"👤 账号: {nickname}")
        lines.append("🔐 凭据已保存到插件数据目录（0600，改配置/升级插件都不会丢）")
        lines.append(f"🔄 桥接: {'已重启并就绪' if bridge_ok else '重启失败，请执行 /dy_bridge_restart'}")
        if verified:
            lines.append(f"🧪 接口验证: {verified}")
        await self._send_to(umo, MessageChain().message("\n".join(lines)))

    def _first_douyin_sec_uid(self) -> str:
        """取当前所有订阅里第一个抖音用户的 sec_uid（用于登录后验证接口）。"""
        try:
            for _umo, records in self.data_manager.get_all_subscriptions().items():
                for rec in records:
                    if getattr(rec, "platform", "douyin") == "douyin":
                        sec = getattr(rec, "sec_uid", "") or getattr(rec, "uid", "")
                        if sec:
                            return str(sec)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"读取订阅找 sec_uid 失败: {e}")
        return ""

    @command("dy_status")
    @permission_type(PermissionType.ADMIN)
    async def dy_status(self, event: AstrMessageEvent):
        """查看插件运行状态（管理员）"""
        cookie_ok = "✅ 已配置" if self.amagi.cookie_configured else "❌ 未配置"
        total_subs = self.subscription_service.get_subscription_count()
        all_subs = self.data_manager.get_all_subscriptions()
        video_subs = sum(1 for rs in all_subs.values() for r in rs if r.sub_type == 'video')
        live_subs = sum(1 for rs in all_subs.values() for r in rs if r.sub_type == 'live')

        listener = self.listener.status_info()
        running = "🟢 运行中" if listener["running"] else "🔴 已停止"

        bridge = self.amagi.status_info()
        bridge_state = "🟢 运行中" if bridge["running"] else "🔴 未运行"
        amagi_ready = "✅ 已就绪" if bridge["built"] else "❌ 未就绪"
        bridge_err = bridge["last_error"] or bridge["build_msg"]

        last_ok_ts = self.data_manager.get_last_success_sub_notify_ts()
        last_ok = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ok_ts))
            if last_ok_ts else "无记录"
        )
        if self.dispatcher.is_silent():
            silent = f"🔇 静默中 (剩余 {self.dispatcher.silent_remaining_secs()} 秒)"
        else:
            silent = "🟢 正常"

        bili_state = "🟢 开启" if self.enable_bilibili else "🔴 关闭"
        bili_cookie = "✅ 已配置" if self.amagi.bilibili_cookie_configured else "❌ 未配置"
        bili_subs = self.subscription_service.get_subscription_count('bilibili')

        # 上游偶发失败的重试统计 / 当前仍在连续失败的订阅项 (便于判断"要不要人工介入")
        retries = int(bridge.get("transient_retries") or 0)
        recovered = int(bridge.get("transient_recovered") or 0)
        if retries:
            upstream = f"↻ 上游偶发失败已重试 {retries} 次 (恢复 {recovered} 次)"
            if bridge.get("last_transient_error"):
                upstream += f", 最近: {bridge['last_transient_error']}"
        else:
            upstream = "✅ 无 (未遇到上游偶发失败)"
        failing = listener.get("failing_checks") or {}
        if failing:
            fail_line = "⚠️ " + ", ".join(f"{k}×{v}" for k, v in list(failing.items())[:3])
        else:
            fail_line = "✅ 无"

        # 凭据来源 (扫码登录保存 / 配置文件) + 扫码登录可用性
        cred_source = self.credentials.source()
        if self.credentials.get_cookie():
            saved_at = self.credentials.saved_at()
            when = time.strftime("%m-%d %H:%M", time.localtime(saved_at)) if saved_at else "时间未知"
            cred_line = (f"扫码登录保存（{self.credentials.nickname() or '未知账号'}，"
                         f"{when} 保存）")
        elif self.cfg.get("douyin_cookie", "").strip() or self.cfg.get("douyin_live_cookie", "").strip():
            cred_line = "插件配置 douyin_cookie"
        else:
            cred_line = "❌ 无（管理员可发送 /dy_login 扫码登录）"
        _ = cred_source

        msg = (
            f"📊 插件运行状态\n"
            f"{'=' * 20}\n"
            f"监听服务: {running}\n"
            f"上次视频扫描: {listener['last_video_scan']}\n"
            f"上次直播扫描: {listener['last_live_scan']}\n"
            f"监听错误: {listener['last_error'] or '无'}\n"
            f"连续失败项: {fail_line}\n"
            f"{'=' * 20}\n"
            f"推送状态: {silent}\n"
            f"上次推送成功: {last_ok}\n"
            f"渲染缓存: {len(self.listener._render_cache)}/{self.listener._render_cache_limit}\n"
            f"{'=' * 20}\n"
            f"Cookie: {cookie_ok}\n"
            f"凭据来源: {cred_line}\n"
            f"扫码登录: {'🔄 进行中' if self._login_busy() else '输入 /dy_login 可用'}\n"
            f"轮询间隔: {self.cfg.get('poll_interval', 60)}秒\n"
            f"直播监控: {'🟢 开启' if self.cfg.get('enable_live_monitor', True) else '🔴 关闭'}\n"
            f"订阅总数: {total_subs} (视频 {video_subs} / 直播 {live_subs})\n"
            f"{'=' * 20}\n"
            f"B站监控: {bili_state} (凭据{bili_cookie}, 订阅 {bili_subs})\n"
            f"B站投稿扫描: {listener['last_bili_video_scan']}\n"
            f"B站直播扫描: {listener['last_bili_live_scan']}\n"
            f"{'=' * 20}\n"
            f"amagi 桥接: {bridge_state} ({bridge['port']})\n"
            f"amagi 运行时: {amagi_ready} (v{bridge['amagi_version']})\n"
            f"上游重试: {upstream}\n"
            f"说明: {bridge_err}"
        )
        yield event.plain_result(msg)

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载时清理"""
        logger.info("amagi 抖音推送插件正在卸载...")
        # 先停扫码登录任务（会顺带关掉无头浏览器、删掉二维码临时图）
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
            try:
                await self._login_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
        if hasattr(self, 'listener'):
            await self.listener.stop()
        # 停止 amagi 桥接进程
        if hasattr(self, 'amagi'):
            try:
                await self.amagi.stop()
            except Exception as e:
                logger.debug(f"停止 amagi 桥接出错: {e}")
        logger.info("amagi 抖音推送插件已卸载")
