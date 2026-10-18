# config.py
from __future__ import annotations

import zoneinfo
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from aiocqhttp import CQHttp

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
    get_astrbot_temp_path,
)


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ============ 插件自定义配置 ==================


class LLMConfig(ConfigNode):
    post_provider_id: str
    post_prompt: str
    comment_provider_id: str
    comment_prompt: str
    reply_provider_id: str
    reply_prompt: str


class SourceConfig(ConfigNode):
    ignore_groups: list[str]
    ignore_users: list[str]
    post_max_msg: int

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)

    def is_ignore_group(self, group_id: str) -> bool:
        return group_id in self.ignore_groups

    def is_ignore_user(self, user_id: str) -> bool:
        return user_id in self.ignore_users


class TriggerConfig(ConfigNode):
    publish_cron: str
    publish_offset: int
    publish_interval_min_hours: int
    publish_interval_max_hours: int
    comment_cron: str
    comment_offset: int
    read_prob: float
    send_admin: bool
    like_when_comment: bool


class AffectionConfig(ConfigNode):
    """
    好感度联动（可选增强）。

    数据取自 astrbot_plugin_self_learning 的好感度库；探测不到时读说说概率
    保持 trigger.read_prob 的固定值，功能静默降级。
    """

    enable: bool
    db_path: str
    cache_ttl: int
    max_prob: float
    curve: float


class QuietHoursConfig(ConfigNode):
    """
    夜间免打扰时段。

    仅拦截「自动行为」——定时评论（AutoComment）、自动发说说（AutoPublish）
    与聊天随机触发的读说说（prob_read_feed）。群友手动发的「评说说 / 赞说说 /
    回复评论」命令、LLM 主动工具、网页面板不受影响。

    时段支持跨零点，如 start=23:00 / end=08:00 表示 23:00 至次日 08:00。
    start 与 end 相等时视为不限制（始终放行）。
    """

    enable: bool
    start: str
    end: str

    @staticmethod
    def _to_minutes(value: str, fallback: int) -> int:
        """把 "HH:MM" 解析成当日分钟数，非法时回退 fallback。"""
        try:
            hh, mm = str(value).strip().split(":", 1)
            hour, minute = int(hh), int(mm)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour * 60 + minute
        except Exception:
            pass
        return fallback

    def start_minutes(self) -> int:
        return self._to_minutes(self.start, 23 * 60)

    def end_minutes(self) -> int:
        return self._to_minutes(self.end, 8 * 60)

    def is_active(self, now: datetime) -> bool:
        """给定的本地时间是否落在免打扰时段内。"""
        if not self.enable:
            return False
        start = self.start_minutes()
        end = self.end_minutes()
        if start == end:
            return False
        current = now.hour * 60 + now.minute
        if start < end:
            return start <= current < end
        # 跨零点：cur >= start（今晚）或 cur < end（明早）
        return current >= start or current < end

    def describe(self) -> str:
        return f"{self.start}–{self.end}"


class PluginConfig(ConfigNode):
    manage_group: str
    llm: LLMConfig
    source: SourceConfig
    trigger: TriggerConfig
    affection: AffectionConfig
    quiet_hours: QuietHoursConfig
    cookie_ttl: int
    timeout: int
    show_name: bool
    allowed_platform_ids: list[str]
    campus_wall_enable: bool

    _DB_VERSION = 5
    _plugin_name = "astrbot_plugin_qzone"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        # 新增开关：老配置文件里没有该键时按「开启」处理，保持与旧版本一致的行为
        cfg.setdefault("campus_wall_enable", True)
        # 好感度联动：老配置缺键时补默认值（默认开启，但探测不到对方插件即静默降级）
        affection = cfg.setdefault("affection", {})
        if isinstance(affection, MutableMapping):
            affection.setdefault("enable", True)
            affection.setdefault("db_path", "")
            affection.setdefault("cache_ttl", 300)
            affection.setdefault("max_prob", 0.05)
            affection.setdefault("curve", 2.0)
        # 夜间免打扰：老配置缺键时补默认值（默认开启 23:00–08:00）
        quiet = cfg.setdefault("quiet_hours", {})
        if isinstance(quiet, MutableMapping):
            quiet.setdefault("enable", True)
            quiet.setdefault("start", "23:00")
            quiet.setdefault("end", "08:00")
        # 自动发说说：从固定钟点改为「上次发布后随机 24~96 小时」，老配置缺键时补默认值
        trigger = cfg.setdefault("trigger", {})
        if isinstance(trigger, MutableMapping):
            trigger.setdefault("publish_interval_min_hours", 24)
            trigger.setdefault("publish_interval_max_hours", 96)
        super().__init__(cfg)
        self.context = context
        # 每个平台适配器实例各自持有一个 CQHttp，避免多 Bot 共用同一个空间身份
        self._clients: dict[str, CQHttp] = {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.temp_dir = Path(get_astrbot_temp_path()) / self._plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / f"posts_{self._DB_VERSION}.db"

        self.emoji_cdn = (
            "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/"
            "img/facebook/64/"
        )
        self.emoji_style = "FACEBOOK"
        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self._normalize_id()
        self.admin_id = self.admins_id[0] if self.admins_id else None
        self.save_config()

    # ==================== 夜间免打扰 ====================

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """
        当前是否处于夜间免打扰时段。

        仅供自动路径（定时评论、自动发说说、聊天随机读说说）调用；手动命令与
        LLM 主动工具不走此判定。判定异常时放行，避免误伤正常功能。
        """
        try:
            return self.quiet_hours.is_active(now or datetime.now(self.timezone))
        except Exception as e:
            logger.debug(f"判定夜间免打扰时段失败，按放行处理: {e}")
            return False

    def quiet_end_datetime(self, now: datetime | None = None) -> datetime | None:
        """
        若当前处于免打扰时段，返回该时段的结束时刻（同一时区）；否则返回 None。

        供定时任务在被静默拦截后改期使用，避免「基准时间恰好落在静默边缘」
        导致当天整轮任务被丢掉。
        """
        current = now or datetime.now(self.timezone)
        try:
            if not self.quiet_hours.is_active(current):
                return None
        except Exception as e:
            logger.debug(f"判定夜间免打扰时段失败，按放行处理: {e}")
            return None
        end = self.quiet_hours.end_minutes()
        candidate = current.replace(
            hour=end // 60, minute=end % 60, second=0, microsecond=0
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate

    # ==================== 多平台（多 Bot）支持 ====================

    def allowed_platforms(self) -> list[str]:
        """平台白名单。为空表示不限制，兼容单 Bot 场景。"""
        raw = self.allowed_platform_ids or []
        return [str(item).strip() for item in raw if str(item).strip()]

    def is_platform_allowed(self, platform_id: str | None) -> bool:
        """事件所属平台是否被允许处理。白名单为空时一律允许。"""
        allowed = self.allowed_platforms()
        if not allowed:
            return True
        return str(platform_id or "") in allowed

    def find_platform(self, platform_id: str):
        """按平台实例 ID 查找 AstrBot 平台适配器。"""
        target = str(platform_id or "")
        if not target:
            return None
        try:
            for inst in self.context.platform_manager.platform_insts:
                if str(getattr(inst.meta(), "id", "")) == target:
                    return inst
        except Exception as e:
            logger.debug(f"查找平台实例 {target} 失败: {e}")
        return None

    def bind_client(self, platform_id: str, bot: CQHttp | None) -> CQHttp | None:
        """登记某平台实例的 CQHttp 客户端。"""
        bot = bot or None
        key = str(platform_id or "")
        if bot is None or not key:
            return None
        self._clients[key] = bot
        return bot

    def resolve_client(self, platform_id: str | None = None) -> CQHttp | None:
        """
        取指定平台的 CQHttp 客户端。

        传入 platform_id 时只返回该平台自己的客户端；不传时返回"默认平台"的客户端
        （Web API 等没有事件上下文的场景使用）。
        """
        key = str(platform_id or "")
        if key:
            bot = self._clients.get(key)
            if bot is not None:
                return bot
            inst = self.find_platform(key)
            bot = getattr(inst, "bot", None) if inst is not None else None
            if bot is not None:
                self._clients[key] = bot
            return bot

        return self.default_client()

    def default_platform_id(self) -> str:
        """默认平台 ID：优先白名单首个，其次首个已绑定的平台，最后兜底第一个 aiocqhttp。"""
        allowed = self.allowed_platforms()
        if allowed:
            return allowed[0]
        if self._clients:
            return next(iter(self._clients))
        inst = self._first_aiocqhttp_platform()
        if inst is not None:
            return str(getattr(inst.meta(), "id", ""))
        return ""

    def default_client(self) -> CQHttp | None:
        """默认平台的客户端，供无事件上下文的调用点使用。"""
        platform_id = self.default_platform_id()
        if platform_id:
            return self.resolve_client(platform_id)
        inst = self._first_aiocqhttp_platform()
        return getattr(inst, "bot", None) if inst is not None else None

    def _first_aiocqhttp_platform(self):
        try:
            platform = self.context.get_platform("aiocqhttp")
            if platform is not None:
                return platform
        except Exception:
            pass
        try:
            for candidate in self.context.platform_manager.platform_insts:
                if getattr(candidate.meta(), "name", "") == "aiocqhttp":
                    return candidate
        except Exception as e:
            logger.debug(f"查找 aiocqhttp 平台失败: {e}")
        return None

    def _normalize_id(self):
        """仅保留纯数字ID"""
        for ids in [
            self.admins_id,
            self.source.ignore_groups,
            self.source.ignore_users,
        ]:
            normalized = []
            for raw in ids:
                s = str(raw)
                if s.isdigit():
                    normalized.append(s)
            ids.clear()
            ids.extend(normalized)

    def append_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if not self.source.is_ignore_user(uid):
                self.source.ignore_users.append(str(uid))
        self.save_config()

    def remove_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if self.source.is_ignore_user(uid):
                self.source.ignore_users.remove(str(uid))
        self.save_config()
