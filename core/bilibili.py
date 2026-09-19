"""
B 站数据适配层 —— 通过 amagi 桥接获取 B 站数据, 并统一成插件内部结构。

走的是 amagi 的旧式路由(与本插件的抖音调用风格一致):
  GET /api/bilibili/fetch_user_dynamic      (methodType=userDynamicList, host_mid=<uid>)
  GET /api/bilibili/fetch_user_live_status  (methodType=userLiveStatus,  host_mid=<uid>)
  GET /api/bilibili/fetch_user_profile      (methodType=userCard,       host_mid=<uid>)

amagi 的响应是两层信封, 这里统一拆到「B 站原始对象」:
  { "success": true, "data": { "code": 0, "message": "0", "data": <B站原始响应> } }

只推送「投稿视频」这一种动态(DYNAMIC_TYPE_AV / MAJOR_TYPE_ARCHIVE) + 直播上下播,
与抖音部分的语义保持一致; 图文/转发/专栏动态默认忽略(需要时可在此扩展)。
"""

import re
from typing import Any, Dict, List, Optional

from astrbot.api import logger

# ============================================================================
# 直播状态语义 (集中在此处, 便于真机实测后校准)
# ============================================================================
# /xlive/web-room/v1/index/getInfoByUser 一类接口返回:
#   liveStatus:  1 = 直播中, 0 = 未开播   ← 主判据
#   roomStatus:  直播间状态 (实测未开播时也可能为 1, 因此只作兜底)
#   roundStatus: 轮播状态
# 若真机实测与你的账号所见不一致, 只需改下面两个常量。
BILI_LIVE_STATUS_ON = 1
BILI_ROOM_STATUS_ON = 1

# 投稿视频动态
VIDEO_DYNAMIC_TYPE = "DYNAMIC_TYPE_AV"
VIDEO_MAJOR_TYPE = "MAJOR_TYPE_ARCHIVE"

# 用户主页 URL / 纯 UID
_SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d+)")
_BARE_UID_RE = re.compile(r"^\d{3,12}$")


def _to_int(value: Any) -> Optional[int]:
    """尽力转 int; 无法转换(含 None/空串/bool)时返回 None"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _https(url: Any) -> str:
    """B 站封面常回 http://, 交给协议端下载前统一升级为 https"""
    text = str(url or "").strip()
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text


def parse_mid(text: str) -> Optional[str]:
    """
    从文本中提取 B 站用户 UID(mid)。

    支持:
    - https://space.bilibili.com/525972018
    - 525972018 (纯数字 UID)
    """
    text = (text or "").strip()
    url_match = _SPACE_URL_RE.search(text)
    if url_match:
        return url_match.group(1)
    if _BARE_UID_RE.match(text):
        return text
    return None


# ============================================================================
# 动态条目解析
# ============================================================================

def _unwrap(payload: Any) -> Any:
    """拆掉 amagi 的两层信封, 返回 B 站原始响应体"""
    if not isinstance(payload, dict):
        return None
    mid = payload.get("data")
    if isinstance(mid, dict) and "data" in mid:
        return mid.get("data")
    return mid


def get_dynamic_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("id_str") or item.get("id") or "")


def get_dynamic_type(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("type") or "")


def get_pub_ts(item: Any) -> int:
    """动态发布时间戳(秒); 拿不到返回 0"""
    modules = item.get("modules") if isinstance(item, dict) else None
    author = modules.get("module_author") if isinstance(modules, dict) else None
    if not isinstance(author, dict):
        return 0
    return _to_int(author.get("pub_ts")) or 0


def is_pinned(item: Any) -> bool:
    """是否为置顶动态(module_tag.text == 置顶)"""
    modules = item.get("modules") if isinstance(item, dict) else None
    tag = modules.get("module_tag") if isinstance(modules, dict) else None
    if not isinstance(tag, dict):
        return False
    return str(tag.get("text") or "").strip() == "置顶"


def is_video_dynamic(item: Any) -> bool:
    """是否为「投稿视频」动态"""
    if get_dynamic_type(item) != VIDEO_DYNAMIC_TYPE:
        return False
    major = (item.get("modules") or {}).get("module_dynamic", {}).get("major") or {}
    if not isinstance(major, dict):
        return False
    # major.type 实测为 MAJOR_TYPE_ARCHIVE; 少数版本可能缺该字段, 此时只要 archive 存在即认
    if major.get("type") and major.get("type") != VIDEO_MAJOR_TYPE:
        return False
    return isinstance(major.get("archive"), dict)


def extract_video(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    把一条投稿视频动态归一化成插件内部结构:

      {
        "dynamic_id": str, "bvid": str, "title": str, "desc": str,
        "cover": str(https), "url": str, "duration": str,
        "play": str, "danmaku": str, "pub_ts": int,
        "author": {"mid": str, "name": str, "face": str(https)},
      }
    """
    if not is_video_dynamic(item):
        return None

    major = (item.get("modules") or {}).get("module_dynamic", {}).get("major") or {}
    archive = major.get("archive") or {}
    author = (item.get("modules") or {}).get("module_author") or {}
    stat = archive.get("stat") if isinstance(archive.get("stat"), dict) else {}

    bvid = str(archive.get("bvid") or "")
    if not bvid:
        return None

    return {
        "dynamic_id": get_dynamic_id(item),
        "bvid": bvid,
        "title": str(archive.get("title") or "无标题"),
        "desc": str(archive.get("desc") or ""),
        "cover": _https(archive.get("cover")),
        "url": f"https://www.bilibili.com/video/{bvid}",
        "duration": str(archive.get("duration_text") or ""),
        "play": str(stat.get("play") or "0"),
        "danmaku": str(stat.get("danmaku") or "0"),
        "pub_ts": get_pub_ts(item),
        "pinned": is_pinned(item),
        "author": {
            "mid": str(author.get("mid") or ""),
            "name": str(author.get("name") or ""),
            "face": _https(author.get("face")),
        },
    }


# ============================================================================
# 请求封装
# ============================================================================

async def get_user_card(amagi, mid: str) -> Optional[dict]:
    """用户名片(card 段): { mid, name, face, fans, sign, ... }"""
    if not mid:
        return None
    payload = await amagi.request(
        "/api/bilibili/fetch_user_profile",
        {"methodType": "userCard", "host_mid": mid},
    )
    body = _unwrap(payload)
    if not isinstance(body, dict):
        return None
    card = body.get("card")
    if not isinstance(card, dict):
        logger.warning(f"B 站用户名片响应缺少 card 字段 (mid={mid})")
        return None
    return card


async def get_user_dynamics(amagi, mid: str) -> Optional[List[dict]]:
    """用户主页动态列表(新 → 旧); 失败/异常时抛 AmagiError 或返回 None"""
    if not mid:
        return None
    payload = await amagi.request(
        "/api/bilibili/fetch_user_dynamic",
        {"methodType": "userDynamicList", "host_mid": mid},
    )
    body = _unwrap(payload)
    if not isinstance(body, dict):
        return None
    items = body.get("items")
    if not isinstance(items, list):
        return None
    return items


async def get_user_videos(amagi, mid: str) -> Optional[List[dict]]:
    """用户最新投稿视频列表(仅 DYNAMIC_TYPE_AV, 已归一化)"""
    items = await get_user_dynamics(amagi, mid)
    if items is None:
        return None
    videos: List[dict] = []
    for item in items:
        video = extract_video(item)
        if video:
            videos.append(video)
    return videos


async def get_live_snapshot(amagi, mid: str) -> Optional[dict]:
    """
    返回规范化的直播快照:

      {
        "mid": str, "is_live": bool, "status_known": bool,
        "status_source": str,          # 判定所用字段
        "room_id": str, "room_title": str, "cover": str(https),
        "online": int, "raw": {...},   # 原始字段, 便于排查/校准
      }

    判定: liveStatus == 1 视为直播中; liveStatus 缺失时用 roomStatus 兜底;
    两个字段都读不到时 status_known=False —— 调用方应视为「状态未知」并跳过本轮,
    不能当作「未开播」(否则接口偶发缺字段会被误判成下播, 来回刷屏)。
    """
    if not mid:
        return None
    payload = await amagi.request(
        "/api/bilibili/fetch_user_live_status",
        {"methodType": "userLiveStatus", "host_mid": mid},
    )
    body = _unwrap(payload)
    if not isinstance(body, dict):
        return None

    live_status = _to_int(body.get("liveStatus"))
    room_status = _to_int(body.get("roomStatus"))

    raw_status: Optional[int] = None
    source = ""
    if live_status is not None:
        raw_status, source = live_status, "liveStatus"
    elif room_status is not None:
        raw_status, source = room_status, "roomStatus"

    status_known = raw_status is not None
    if not status_known:
        is_live = False
    elif source == "liveStatus":
        is_live = raw_status == BILI_LIVE_STATUS_ON
    else:
        is_live = raw_status == BILI_ROOM_STATUS_ON

    room_id = body.get("roomid") or body.get("room_id") or ""
    return {
        "mid": mid,
        "is_live": is_live,
        "status_known": status_known,
        "status_source": source,
        "room_id": str(room_id) if room_id else "",
        "room_title": str(body.get("title") or ""),
        "cover": _https(body.get("cover")),
        "online": _to_int(body.get("online")) or 0,
        "url": _https(body.get("url")) or (f"https://live.bilibili.com/{room_id}" if room_id else ""),
        "raw": {
            "liveStatus": body.get("liveStatus"),
            "roomStatus": body.get("roomStatus"),
            "roundStatus": body.get("roundStatus"),
        },
    }
