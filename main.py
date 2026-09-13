import asyncio
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import command, permission_type, PermissionType
from astrbot.core.star.filter.command import GreedyStr

from .core.data_manager import DataManager
from .core.douyin import (
    get_live_snapshot,
    get_user_nickname,
    get_user_profile,
    get_user_works,
)
from .core.models import SubscriptionRecord
from .core.utils import build_user_url, format_number, parse_sec_uid
from .services.amagi_service import AmagiService
from .services.listener import DouyinListener
from .services.renderer import Renderer
from .services.subscription_service import SubscriptionService

# 插件根目录
plugin_dir = Path(__file__).parent

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
    "1.0.1",
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
        cookie = (self.cfg.get("douyin_cookie", "").strip()
                  or self.cfg.get("douyin_live_cookie", "").strip())
        self.amagi.set_cookie(cookie)

        # 3. 初始化渲染器
        self.rai = self.cfg.get("rai", False)
        self.renderer = Renderer(star=self, rai=self.rai)

        # 4. 初始化订阅服务
        self.subscription_service = SubscriptionService(self.data_manager)

        # 5. 初始化监听服务
        self.listener = DouyinListener(
            context=self.context,
            data_manager=self.data_manager,
            amagi=self.amagi,
            renderer=self.renderer,
            cfg=self.cfg
        )

        # 6. 后台准备并启动 amagi 桥接 (运行时缺失时自动 npm 安装)
        self._amagi_task: Optional[asyncio.Task] = None
        asyncio.create_task(self._boot_amagi())

        # 7. 启动后台监听
        self._listener_task: Optional[asyncio.Task] = None
        self._start_listener()

        if not cookie:
            logger.warning("⚠️ 抖音 Cookie 未配置, 请先在插件设置中配置 douyin_cookie")

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

    def _restart_listener(self):
        """重启监听服务"""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
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

        self._restart_listener()
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
                self._restart_listener()
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
                self._restart_listener()
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
                detail = f" (raw room_status={snap.get('room_status')})" if snap.get("room_status") is not None else ""
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

                # 信任 API 顺序，取第一个（最新）
                latest = works[0]
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

    @command("dy_clear")
    @permission_type(PermissionType.ADMIN)
    async def dy_clear(self, event: AstrMessageEvent):
        """清空当前会话的所有订阅（管理员）"""
        sub_user = event.unified_msg_origin
        msg = await self.subscription_service.remove_all_for_user(sub_user)
        self._restart_listener()
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

        用法: /dy_global_unsub <会话UMO> <UID>
        """
        args = raw_args.strip().split() if raw_args.strip() else []
        if len(args) < 2:
            yield event.plain_result("❌ 用法: /dy_global_unsub <会话UMO> <UID>")
            return
        target_user = args[0]
        target_uid = args[1]
        for st in ['video', 'live']:
            self.data_manager.remove_subscription(target_user, target_uid, st)
        self._restart_listener()
        yield event.plain_result(f"✅ 已移除 {target_user} 的 {target_uid} 订阅")

    @command("dy_bridge_restart")
    @permission_type(PermissionType.ADMIN)
    async def dy_bridge_restart(self, event: AstrMessageEvent):
        """重启 amagi 数据桥接（管理员；改 Cookie 后调用，也会重新检查/安装 amagi 运行时）"""
        # 重新读取配置中的 Cookie (兼容 AstrBot 运行期改配置)
        cookie = (self.cfg.get("douyin_cookie", "").strip()
                  or self.cfg.get("douyin_live_cookie", "").strip())
        self.amagi.set_cookie(cookie)
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
                f"cookie={'已配置' if self.amagi.cookie_configured else '未配置'})"
            )
        else:
            yield event.plain_result(
                f"❌ amagi 桥接重启失败。\n"
                f"状态: {self.amagi.status_info().get('build_msg', '')}\n"
                f"错误: {self.amagi.status_info().get('last_error') or '无'}"
            )

    @command("dy_status")
    @permission_type(PermissionType.ADMIN)
    async def dy_status(self, event: AstrMessageEvent):
        """查看插件运行状态（管理员）"""
        cookie_ok = "✅ 已配置" if self.amagi.cookie_configured else "❌ 未配置"
        total_subs = self.subscription_service.get_subscription_count()
        running = "🟢 运行中" if (self._listener_task and not self._listener_task.done()) else "🔴 已停止"

        bridge = self.amagi.status_info()
        bridge_state = "🟢 运行中" if bridge["running"] else "🔴 未运行"
        amagi_ready = "✅ 已就绪" if bridge["built"] else "❌ 未就绪"
        bridge_err = bridge["last_error"] or bridge["build_msg"]

        msg = (
            f"📊 插件运行状态\n"
            f"{'=' * 20}\n"
            f"运行状态: {running}\n"
            f"Cookie: {cookie_ok}\n"
            f"轮询间隔: {self.cfg.get('poll_interval', 60)}秒\n"
            f"直播监控: {'🟢 开启' if self.cfg.get('enable_live_monitor', True) else '🔴 关闭'}\n"
            f"订阅总数: {total_subs}\n"
            f"{'=' * 20}\n"
            f"amagi 桥接: {bridge_state} ({bridge['port']})\n"
            f"amagi 运行时: {amagi_ready} (v{bridge['amagi_version']})\n"
            f"说明: {bridge_err}"
        )
        yield event.plain_result(msg)

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载时清理"""
        logger.info("amagi 抖音推送插件正在卸载...")
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
