"""
消息渲染器 —— 支持图文渲染和纯文本两种模式。

图文模式（rai=true）：
  使用 HTML 模板 + AstrBot 内置 html_render 生成卡片图片。
纯文本模式（rai=false）：
  直接返回格式化的纯文本消息。
"""

from pathlib import Path
from typing import Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import Star

from ..core.models import LiveInfo, UserInfo, VideoInfo
from ..core.utils import build_live_url, build_video_url, first_url, format_number

# 插件根目录
plugin_dir = Path(__file__).resolve().parent.parent

# ==================== 卡片渲染选项 ====================
#
# 为什么不用「PNG + 高分屏放大」：
#   推送图片时, 图片由 AstrBot 读成 base64 交给协议端(NapCat), 再由 QQ 上传到
#   腾讯富媒体服务器。图片体积过大时这一步会直接失败, 报
#   `rich media transfer failed` (retcode 1200), 整条推送(含链接)一起丢。
# 因此这里刻意选更小的输出:
#   - type=jpeg      : 无损 PNG 的体积通常是 JPEG 的数倍
#   - scale=css      : 不再按高分屏放大(device/ultra), 避免渲染出 2~3 倍分辨率的巨图
# 若仍偶发失败, 可把 quality 继续下调(如 50)进一步减小体积。
CARD_RENDER_OPTIONS = {
    "full_page": True,
    "type": "jpeg",
    "quality": 75,
    "scale": "css",
}

# ==================== 纯文本消息模板 ====================

VIDEO_TEXT_TEMPLATE = """📹 新视频发布
👤 {nickname}
📝 {desc}
❤️ {digg_count} 👍 {comment_count} 💬 {collect_count} ⭐
🔗 {url}"""

LIVE_START_TEXT = """🔴 {nickname} 开播啦！
📺 {title}
🔗 {url}"""

LIVE_END_TEXT = """⚫ {nickname} 已下播
📺 本次直播: {title}"""


class Renderer:
    """消息渲染器"""

    def __init__(self, star: Star, rai: bool = False):
        self.star = star
        self.rai = rai
        self._templates = {}
        # 最近一次渲染结果 (供 /dy_status、/dy_img_test 诊断图片格式与体积)
        self.last_card_path: Optional[str] = None
        self.last_card_size: int = 0
        self.last_card_format: str = ""

    def _load_template(self, name: str) -> Optional[str]:
        """加载 HTML 模板"""
        if name in self._templates:
            return self._templates[name]
        tmpl_path = plugin_dir / "assets" / "templates" / name
        if not tmpl_path.exists():
            return None
        try:
            with open(tmpl_path, 'r', encoding='utf-8') as f:
                content = f.read()
                self._templates[name] = content
                return content
        except Exception as e:
            logger.error(f"加载模板 {name} 失败: {e}")
            return None

    async def _render_card(self, tmpl_name: str, data: dict) -> Optional[str]:
        """使用 AstrBot 内置 html_render 渲染卡片图片"""
        tmpl_str = self._load_template(tmpl_name)
        if not tmpl_str:
            return None
        if not self.rai:
            return None
        try:
            img_path = await self.star.html_render(
                tmpl=tmpl_str,
                data=data,
                return_url=False,
                options=CARD_RENDER_OPTIONS,
            )
            if img_path:
                fmt, size, head, complete = self._sniff_image(img_path)
                if not fmt or not size or not complete:
                    # 渲染服务异常时可能返回 HTML/JSON 错误页或半截数据。这类内容不是有效图片,
                    # 协议端(QQ)会在富媒体上传阶段直接失败(rich media transfer failed),
                    # 发出去只会白挨一次报错, 因此这里直接放弃图片、降级为纯文本。
                    reason = "数据不完整(疑似被截断)" if (fmt and not complete) else "不是图片"
                    logger.warning(
                        f"卡片渲染结果{reason} (格式={fmt or '未知'}, {size} 字节, "
                        f"前 16 字节: {head or '空'}), 文件: {img_path} —— "
                        f"多为 t2i 渲染服务返回了错误内容或下载不完整, 本次降级为纯文本"
                    )
                    return None
                self.last_card_path = img_path
                self.last_card_size = size
                self.last_card_format = fmt
                logger.info(f"卡片渲染成功: {img_path} ({fmt}, {size / 1024:.0f} KB)")
            return img_path
        except Exception as e:
            logger.warning(f"卡片渲染失败，降级为纯文本: {e}")
            return None

    @staticmethod
    def _sniff_image(img_path: str) -> Tuple[str, int, str, bool]:
        """
        校验渲染结果是不是**完整**的图片。

        返回 (格式, 字节数, 前16字节hex, 是否完整)。
        读不到文件或格式无法识别时格式为空串。
        除了 magic bytes, 还检查文件尾 (PNG 的 IEND / JPEG 的 FFD9),
        用来发现"下载了一半"的图片 —— 这种图照样能通过头部检查, 但上传必定失败。
        """
        try:
            size = Path(img_path).stat().st_size
            with open(img_path, "rb") as f:
                head = f.read(16)
                f.seek(max(0, size - 16))
                tail = f.read(16)
        except OSError:
            return "", 0, "", False

        fmt = ""
        complete = True
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt = "PNG"
            complete = b"IEND" in tail
        elif head.startswith(b"\xff\xd8\xff"):
            fmt = "JPEG"
            complete = tail.endswith(b"\xff\xd9")
        elif head.startswith(b"GIF8"):
            fmt = "GIF"
            complete = tail.endswith(b";")
        elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            fmt = "WEBP"
        elif head.startswith(b"BM"):
            fmt = "BMP"
        elif head[4:12] in (b"ftypavif", b"ftypavis"):
            fmt = "AVIF"

        return fmt, size, head.hex(" "), complete

    async def render_video(self, work: dict, nickname: str = "") -> Tuple[str, Optional[str]]:
        """
        渲染视频消息。返回 (消息文本, 可选的图片路径)
        """
        author = work.get('author', {})
        nickname = nickname or author.get('nickname', '未知')
        aweme_id = str(work.get('aweme_id', ''))
        desc = work.get('desc', '无标题')
        statistics = work.get('statistics', {})
        cover = first_url(work.get('video', {}).get('cover'))
        avatar = first_url(author.get('avatar_thumb'))
        digg = format_number(statistics.get('digg_count', 0))
        comment = format_number(statistics.get('comment_count', 0))
        collect = format_number(statistics.get('collect_count', 0))
        share = format_number(statistics.get('share_count', 0))
        url = build_video_url(aweme_id)

        text = VIDEO_TEXT_TEMPLATE.format(
            nickname=nickname, desc=desc[:100],
            digg_count=digg, comment_count=comment,
            collect_count=collect, url=url,
        )

        img_path = await self._render_card("video_card.html", {
            "nickname": nickname,
            "avatar": avatar,
            "cover": cover,
            "title": desc[:100],
            "digg_count": digg,
            "comment_count": comment,
            "collect_count": collect,
            "share_count": share,
            "url": url,
        })

        return text, img_path

    async def render_live(self, record, is_live: bool, title: str = "",
                          avatar: str = "", work: Optional[dict] = None) -> Tuple[str, Optional[str]]:
        """
        渲染直播消息。返回 (消息文本, 可选的图片路径)

        新版直播订阅锚定用户(sec_uid), 无房间号时链接指向用户主页。
        """
        nickname = record.nickname or getattr(record, 'sec_uid', None) or record.uid
        title = title or "无标题"

        # 链接: 有房间号(旧数据/直播间直链)用直播间, 否则用用户主页
        room_id = getattr(record, 'room_id', None) or None
        if room_id:
            url = build_live_url(room_id)
        elif getattr(record, 'sec_uid', None):
            url = build_user_url(record.sec_uid)
        else:
            url = ""

        text = (LIVE_START_TEXT if is_live else LIVE_END_TEXT).format(
            nickname=nickname, title=title, url=url,
        )

        if not avatar and work:
            avatar = first_url(work.get('author', {}).get('avatar_thumb'))

        img_path = await self._render_card("live_card.html", {
            "badge_class": "live-badge" if is_live else "offline-badge",
            "badge_text": "🔴 直播中" if is_live else "⭕ 已下播",
            "nickname": nickname,
            "avatar": avatar,
            "title": title,
            "url": url,
        })

        return text, img_path

    def render_user_info(self, user: UserInfo) -> str:
        """渲染用户信息（仅纯文本）"""
        user_url = user.user_url or build_user_url(user.sec_uid)
        return (
            f"👤 {user.nickname}\n"
            f"📝 {user.signature[:50] if user.signature else '这个人很懒，什么都没写'}\n"
            f"👥 粉丝: {format_number(user.follower_count)}  ·  关注: {format_number(user.following_count)}\n"
            f"❤️ 获赞: {format_number(user.total_favorited)}  ·  作品: {user.aweme_count}\n"
            f"🔗 {user_url}"
        )


def build_user_url(sec_uid: str) -> str:
    """构建抖音用户主页 URL"""
    return f"https://www.douyin.com/user/{sec_uid}"