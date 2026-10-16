"""好感度数据源（可选增强）。

只读复用 `astrbot_plugin_self_learning` 的好感度数据，把「随机读说说」的概率
按发言者的好感度抬高。

设计前提：该插件不是本插件的依赖，所以这里所有路径都必须能「优雅缺席」——
探测不到就返回 None，调用方按配置里的固定概率继续跑，异常一律不上抛。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

if TYPE_CHECKING:
    from .config import PluginConfig

# 好感度数据来自这个插件（本插件只读它的库，不依赖它的代码）
SOURCE_PLUGIN_NAME = "astrbot_plugin_self_learning"
DB_FILENAME = "messages.db"
AFFECTION_TABLE = "user_affections"

_DEFAULT_CACHE_TTL = 300
_DEFAULT_MAX_PROB = 0.05
_DEFAULT_CURVE = 2.0
_MAX_CACHE_ENTRIES = 2048


class AffectionSource:
    """按 (群号, QQ号) 读取好感度，并换算成读说说的概率。

    - 结果按 TTL 缓存，避免每条群消息都打一次磁盘；
    - 探测结果同样缓存，未安装该插件的部署不会反复 open 文件；
    - 连接一律 `mode=ro`，只读打开，不影响对方插件写入。
    """

    def __init__(self, cfg: "PluginConfig") -> None:
        self.cfg = cfg
        # (group_id, user_id) -> (缓存时间, (好感度, 上限) 或 None)
        self._levels: dict[tuple[str, str], tuple[float, tuple[int, int] | None]] = {}
        self._available: bool | None = None
        self._next_probe: float = 0.0
        self._logged: bool = False

    # ==================== 配置读取 ====================

    def _opt(self, name: str, default):
        """读好感度配置项；老配置文件缺键时回退到默认值。"""
        try:
            value = getattr(self.cfg.affection, name)
        except Exception:
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._opt("enable", True))

    @property
    def cache_ttl(self) -> int:
        try:
            return max(int(self._opt("cache_ttl", _DEFAULT_CACHE_TTL)), 10)
        except (TypeError, ValueError):
            return _DEFAULT_CACHE_TTL

    def db_path(self) -> Path:
        raw = str(self._opt("db_path", "") or "").strip()
        if raw:
            return Path(raw)
        return Path(get_astrbot_plugin_data_path()) / SOURCE_PLUGIN_NAME / DB_FILENAME

    # ==================== 探测 ====================

    def _connect(self, path: Path) -> sqlite3.Connection:
        """只读打开，短超时：对方插件正在写库时也不能把我们挂住。"""
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)

    def _table_exists(self, path: Path) -> bool:
        try:
            con = self._connect(path)
        except Exception as e:
            logger.debug(f"打开好感度库失败: {e}")
            return False
        try:
            row = con.execute(
                "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
                (AFFECTION_TABLE,),
            ).fetchone()
        except Exception as e:
            logger.debug(f"查询好感度表失败: {e}")
            return False
        finally:
            con.close()
        return row is not None

    async def detect(self) -> bool:
        """好感度数据是否可用（结果带缓存，按 cache_ttl 重新探测）。"""
        if not self.enabled:
            return False

        now = time.time()
        if self._available is not None and now < self._next_probe:
            return self._available

        path = self.db_path()
        available = path.is_file() and await asyncio.to_thread(
            self._table_exists, path
        )
        self._available = available
        self._next_probe = now + self.cache_ttl

        if not self._logged:
            # 只在首次探测时各打一条，避免刷屏
            if available:
                logger.info(
                    f"已检测到 {SOURCE_PLUGIN_NAME} 的好感度数据"
                    f"（{path}），读说说概率将按好感度加权"
                )
            else:
                logger.info(
                    f"未检测到 {SOURCE_PLUGIN_NAME} 的好感度数据"
                    f"（{path}），读说说概率使用固定值"
                )
            self._logged = True
        return available

    # ==================== 读取 ====================

    def _query(self, group_id: str, user_id: str) -> tuple[int, int] | None:
        path = self.db_path()
        try:
            con = self._connect(path)
        except Exception as e:
            logger.debug(f"打开好感度库失败: {e}")
            return None
        try:
            row = con.execute(
                f"select affection_level, max_affection from {AFFECTION_TABLE} "
                "where group_id = ? and user_id = ? limit 1",
                (group_id, user_id),
            ).fetchone()
        except Exception as e:
            logger.debug(f"读取好感度失败: {e}")
            return None
        finally:
            con.close()

        if not row:
            return None
        try:
            return int(row[0]), int(row[1])
        except (TypeError, ValueError):
            return None

    async def level(self, group_id: str, user_id: str) -> tuple[int, int] | None:
        """取某人在某群的好感度，返回 (好感度, 上限)；取不到时返回 None。"""
        if not group_id or not user_id:
            return None
        if not await self.detect():
            return None

        key = (str(group_id), str(user_id))
        now = time.time()
        cached = self._levels.get(key)
        if cached is not None and now - cached[0] < self.cache_ttl:
            return cached[1]

        value = await asyncio.to_thread(self._query, key[0], key[1])
        if len(self._levels) >= _MAX_CACHE_ENTRIES:
            self._levels.clear()
        self._levels[key] = (now, value)
        return value

    # ==================== 概率换算 ====================

    def read_probability(
        self,
        base: float,
        affection: tuple[int, int] | None,
    ) -> float:
        """把基础概率按好感度抬高，上限为配置里的 max_prob。

        好感度比例 r = 好感度 / 上限，实际概率 = base + (max_prob - base) * r^curve。
        curve > 1 时曲线偏保守：只有好感度较高的用户才会被明显提高。
        """
        base = max(float(base or 0.0), 0.0)
        if affection is None:
            return base

        level, max_level = affection
        if max_level <= 0:
            return base

        try:
            target = max(float(self._opt("max_prob", _DEFAULT_MAX_PROB)), base)
            curve = max(float(self._opt("curve", _DEFAULT_CURVE)), 0.1)
        except (TypeError, ValueError):
            target, curve = max(_DEFAULT_MAX_PROB, base), _DEFAULT_CURVE

        ratio = min(max(level / max_level, 0.0), 1.0)
        return base + (target - base) * (ratio**curve)
