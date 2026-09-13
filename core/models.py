from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SubscriptionRecord:
    """订阅记录"""
    sub_user: str                       # 会话标识（如 group_123456）
    uid: str                            # 抖音 sec_uid
    sub_type: str                       # 'video' 或 'live'
    # 订阅时的标识信息
    sec_uid: str = ""                   # 用户的 sec_uid（视频与直播监控共用）
    nickname: str = ""                  # 缓存的用户昵称
    # 视频监控状态
    last_video_id: Optional[str] = None  # 最后推送的视频ID
    # 直播监控状态
    is_live: bool = False               # 当前是否开播
    last_live_title: str = ""           # 上次直播标题
    room_id: str = ""                   # 直播间 room_id（由用户主页接口解析）
    # @全体成员 选项
    at_all: bool = False               # 开播/发视频时 @全体成员
    live_atall: bool = False           # 仅开播时 @全体成员
    # 已推送的 ID 缓存（防止重复推送）
    recent_ids: List[str] = field(default_factory=list)  # 最近推送的视频ID列表
    # 过滤选项
    filter_keywords: List[str] = field(default_factory=list)  # 过滤关键词


@dataclass
class VideoInfo:
    """视频信息"""
    aweme_id: str                       # 作品ID
    title: str                          # 标题/描述
    share_url: str                      # 分享链接
    cover_url: Optional[str] = None     # 封面图
    create_time: Optional[int] = None   # 创建时间戳
    digg_count: int = 0                 # 点赞数
    comment_count: int = 0              # 评论数
    nickname: str = ""                  # 作者昵称
    author_avatar: str = ""             # 作者头像


@dataclass
class LiveInfo:
    """直播信息"""
    room_id: str                        # 直播间ID
    title: str                          # 直播标题
    status: int                         # 2: 直播中, 4: 未开播
    user_id: Optional[str] = None       # 用户ID
    nickname: Optional[str] = None      # 主播昵称
    cover_url: Optional[str] = None     # 封面图
    sec_uid: Optional[str] = None       # 用户sec_uid


@dataclass
class UserInfo:
    """用户信息"""
    sec_uid: str                        # 用户唯一标识
    nickname: str                       # 昵称
    avatar_url: str                     # 头像URL
    signature: str = ""                 # 签名
    follower_count: int = 0             # 粉丝数
    following_count: int = 0            # 关注数
    total_favorited: int = 0            # 获赞数
    aweme_count: int = 0                # 作品数
    user_url: str = ""                  # 主页URL