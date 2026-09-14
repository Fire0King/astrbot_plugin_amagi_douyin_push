"""
抖音数据适配层 —— 通过 amagi 桥接获取抖音数据, 并统一成插件内部结构。

所有真实请求都经 services.amagi_service.AmagiService 转发到常驻的 amagi HTTP 服务:
  GET /api/douyin/fetch_user_info        (methodType=userProfile)
  GET /api/douyin/fetch_user_post_videos (methodType=userVideoList)

amagi 返回抖音网页版原始响应, 这里统一为插件内部结构:
  - 用户资料: { "user": {...}, ... }
  - 作品列表: { "aweme_list": [...] }
"""

from typing import Any, Dict, Optional

from astrbot.api import logger

from .utils import first_url

# ============================================================================
# 直播状态语义 (集中在此处, 便于真机实测后校准)
# ============================================================================
#
# 抖音直播状态存在两套数字约定, 容易混淆:
#   1) 直播间数据里的 status (webcast/room/web/enter 返回的 room.status,
#      以及落地页 SSR 的 room.status):  2 = 直播中, 4 = 未开播
#      —— DouyinLiveRecorder / aio-dynamic-push 等同类项目均采用此约定。
#   2) 用户对象里的 live_status (用户主页 /aweme/v1/web/user/profile/other 返回的
#      user.live_status): 社区通行约定 1 = 正在直播, 其余/缺失 = 未直播。
#
# 本插件按「订阅用户」(sec_uid) 轮询用户主页 (方案 B), 因此以 live_status 为主;
# 若响应中同时带 live_room 对象 (含 status, 按 2/4 约定), 则优先用 live_room。
# 若真机实测与你账号所见不一致, 只需要改下面两个常量或上面的判定顺序。
USER_LIVE_STATUS_ON = 1        # user.live_status == 1 视为直播中
ROOM_STATUS_LIVE = 2           # live_room.status == 2 视为直播中 (webcast 约定)


def _to_int(value: Any) -> Optional[int]:
    """尽力转 int; 无法转换(含 None/空串/异常类型)时返回 None"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_aweme_id(item: Any) -> str:
    """取作品 ID (非字典/缺失时返回空串)"""
    if not isinstance(item, dict):
        return ""
    return str(item.get("aweme_id") or "")


def get_create_time(item: Any) -> int:
    """取作品发布时间戳; 拿不到时返回 0"""
    if not isinstance(item, dict):
        return 0
    return _to_int(item.get("create_time")) or 0


def is_pinned(item: Any) -> bool:
    """
    是否为置顶作品。

    抖音用户作品列表会把置顶作品排在列表最前面（且它们是旧作, 不是最新作品）,
    列表项带 is_top: 1 标记。判定新作品时不能依赖列表顺序, 否则会被置顶作品干扰。
    """
    if not isinstance(item, dict):
        return False
    return (_to_int(item.get("is_top")) or 0) == 1


# ============================================================================
# 用户资料
# ============================================================================

async def get_user_profile(amagi, sec_uid: str) -> Optional[dict]:
    """
    获取用户主页原始响应 (含 user 字段), 失败/异常时抛 AmagiError 或返回 None。
    """
    if not sec_uid:
        return None
    data = await amagi.request(
        "/api/douyin/fetch_user_info",
        {"methodType": "userProfile", "sec_uid": sec_uid},
    )
    if not isinstance(data, dict) or "user" not in data:
        logger.warning(f"用户主页响应缺少 user 字段 (sec_uid={sec_uid})")
        return None
    return data


async def get_user_nickname(amagi, sec_uid: str) -> Optional[str]:
    profile = await get_user_profile(amagi, sec_uid)
    if not profile:
        return None
    user = profile.get("user") or {}
    return user.get("nickname") or None


# ============================================================================
# 视频作品
# ============================================================================

async def get_user_works(amagi, sec_uid: str, number: int = 18) -> Optional[list]:
    """
    获取用户最新作品列表 (按 API 返回顺序, 新 -> 旧)。

    amagi 对 userVideoList 的分页以 number 为目标, 单次请求最多 18 条,
    这里固定 number<=18 只拉第一页。
    """
    if not sec_uid:
        return None
    number = max(1, min(int(number or 18), 18))
    data = await amagi.request(
        "/api/douyin/fetch_user_post_videos",
        {"methodType": "userVideoList", "sec_uid": sec_uid, "number": number},
    )
    if not isinstance(data, dict):
        return None
    works = data.get("aweme_list") or []
    return works if isinstance(works, list) else None


# ============================================================================
# 直播状态 (按用户轮询)
# ============================================================================

async def get_live_snapshot(amagi, sec_uid: str) -> Optional[dict]:
    """
    轮询用户主页, 返回规范化的直播快照:
      {
        "sec_uid":     str,
        "nickname":    str,
        "avatar":      str,
        "is_live":     bool,          # 仅在 status_known=True 时有意义
        "status_known":bool,          # 是否真的读到了直播状态字段
        "status_source":str,          # 判定所用字段: live_room.status / user.live_status
        "room_id":     str,           # 用户直播间内部 id (room_id_str), 未开播也可能有值
        "room_title":  str,
        "room_status": int | None,    # 主页能拿到的直播状态原始值, 便于排查
      }

    判定逻辑:
      1) 若主页返回 live_room 对象且带 status (2=直播中/4=未开播), 用 room.status == 2 判定;
      2) 否则用 user.live_status == 1 判定;
      3) 两者都拿不到时 status_known=False —— 调用方应视为「状态未知」并跳过本轮,
         不能当作「未开播」(否则会造成误报下播、来回刷屏)。
    """
    if not sec_uid:
        return None
    profile = await get_user_profile(amagi, sec_uid)
    if not profile:
        return None

    user = profile.get("user") or {}
    nickname = str(user.get("nickname") or sec_uid)
    avatar = first_url(user.get("avatar_thumb"))
    room_id = str(user.get("room_id_str") or user.get("room_id") or "")

    live_room = user.get("live_room")
    raw_status: Optional[int] = None
    status_source = ""
    room_title = ""

    if isinstance(live_room, dict):
        # 个别版本的抖音在直播时会回填 live_room (含 status/title/cover)
        room_status = _to_int(live_room.get("status"))
        if room_status is not None:
            raw_status = room_status
            status_source = "live_room.status"
            room_title = str(live_room.get("title") or "")
            if not room_id:
                room_id = str(live_room.get("room_id_str") or live_room.get("room_id") or "")

    if raw_status is None:
        user_live_status = _to_int(user.get("live_status"))
        if user_live_status is not None:
            raw_status = user_live_status
            status_source = "user.live_status"

    # 判定 (语义见文件头注释, 真机实测后可在此微调)
    # 注意: 两个字段都缺失/无法解析时 status_known=False, 此时**不能**当作「未开播」,
    # 否则接口偶发缺字段会被误判成下播, 造成「下播↔开播」来回刷屏。
    status_known = raw_status is not None
    if not status_known:
        is_live = False
    elif status_source == "live_room.status":
        is_live = raw_status == ROOM_STATUS_LIVE
    else:
        is_live = raw_status == USER_LIVE_STATUS_ON

    return {
        "sec_uid": sec_uid,
        "nickname": nickname or sec_uid,
        "avatar": avatar,
        "is_live": is_live,
        "status_known": status_known,
        "status_source": status_source,
        "room_id": room_id,
        "room_title": room_title,
        "room_status": raw_status,
        "live_room": live_room if isinstance(live_room, dict) else None,
    }
