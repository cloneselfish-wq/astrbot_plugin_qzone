# core/memory.py
"""
从 astrbot_plugin_livingmemory 的 SQLite 记忆库里取「近期真实经历」，
作为自动发说说的素材。

设计约束：
- **只读**：以 `mode=ro` 打开对方的库，不写入、不迁移、不加锁；
- **可降级**：库不存在 / 表结构不符 / 读取超时，一律返回空列表，
  自动发说说退回原来的「随机群聊主题」路径；
- **不重复**：按 documents.id 单调递增记水位，发布成功后才推进，
  失败不吞素材。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)

from .config import MemoryConfig, PluginConfig

_LIVINGMEMORY_DIR = "astrbot_plugin_livingmemory"
_LIVINGMEMORY_DB = "livingmemory.db"

_MC_PLUGIN_DIR = "astrbot_plugin_mc_live"
_MC_SHOT_DIR = Path("shots") / "incoming"

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


@dataclass
class MemoryItem:
    """一条记忆摘要（来自 livingmemory.documents）。"""

    doc_id: int
    created_at: datetime
    summary: str
    topics: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    importance: float = 0.0
    start: datetime | None = None
    end: datetime | None = None

    @property
    def time_label(self) -> str:
        """人类可读的时间标签，例如 `10-18 20:13`。"""
        return self.created_at.strftime("%m-%d %H:%M")


class LivingMemorySource:
    """livingmemory 记忆库的只读访问层。"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.memory_cfg: MemoryConfig = config.memory

    # ==================== 路径解析 ====================

    def db_path(self) -> Path:
        raw = str(self.memory_cfg.db_path or "").strip()
        if raw:
            return Path(raw)
        return (
            Path(get_astrbot_plugin_data_path())
            / _LIVINGMEMORY_DIR
            / _LIVINGMEMORY_DB
        )

    def screenshot_dir(self) -> Path:
        raw = str(self.memory_cfg.screenshot_dir or "").strip()
        if raw:
            return Path(raw)
        return Path(get_astrbot_plugin_path()) / _MC_PLUGIN_DIR / _MC_SHOT_DIR

    def persona(self, platform_id: str) -> str:
        """记忆归属人格：默认与平台实例 ID 同名（如「山吹沙绫」）。"""
        raw = str(self.memory_cfg.persona or "").strip()
        return raw or str(platform_id or "")

    # ==================== 读取 ====================

    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_snapshot(self, path: Path) -> tuple[sqlite3.Connection, Path]:
        """只读打开失败时（例如 WAL 需要写 -shm），退化为读临时快照。"""
        tmp_dir = Path(tempfile.mkdtemp(prefix="qzone_mem_"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.copy2(src, tmp_dir / (path.name + suffix))
        return self._connect(tmp_dir / path.name), tmp_dir

    @staticmethod
    def _parse_dt(raw: object) -> datetime | None:
        if not raw:
            return None
        text = str(raw).strip().replace("Z", "+00:00")
        for candidate in (text, text.replace(" ", "T", 1)):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
        return None

    def _fetch_sync(
        self,
        *,
        persona: str,
        since_id: int,
        lookback_hours: int,
        limit: int,
    ) -> list[MemoryItem]:
        path = self.db_path()
        if not path.is_file():
            logger.warning(f"[memory] 记忆库不存在，跳过记忆素材：{path}")
            return []

        keywords = [
            str(k).strip()
            for k in (self.memory_cfg.keywords or [])
            if str(k).strip()
        ]
        if not keywords:
            return []

        cutoff = (datetime.now() - timedelta(hours=max(1, lookback_hours))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        kw_clause = " OR ".join("text LIKE ?" for _ in keywords)
        sql = f"""
            SELECT id, created_at, text, metadata
              FROM documents
             WHERE json_extract(metadata, '$.session_id') LIKE ?
               AND id > ?
               AND created_at >= ?
               AND ({kw_clause})
             ORDER BY id DESC
             LIMIT ?
        """
        # 多取一些候选，再做「主题命中」打分筛选，避免把只是顺口提到 MC 的闲聊选进来
        candidate_limit = max(8, max(1, limit) * 4)
        params: list[object] = [
            f"{persona}:%" if persona else "%",
            int(since_id or 0),
            cutoff,
            *[f"%{k}%" for k in keywords],
            candidate_limit,
        ]

        tmp_dir: Path | None = None
        try:
            try:
                conn = self._connect(path)
            except sqlite3.Error as e:
                logger.info(f"[memory] 直接只读失败（{e}），改用临时快照读取")
                conn, tmp_dir = self._connect_snapshot(path)

            with conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.warning(f"[memory] 读取记忆库失败，本次跳过记忆素材：{e}")
            return []
        finally:
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        items: list[MemoryItem] = []
        max_chars = max(80, int(self.memory_cfg.max_chars_per_item or 400))
        for row in rows:
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            raw_summary = str(meta.get("canonical_summary") or row["text"] or "").strip()
            # livingmemory 的 canonical_summary 形如「正文 | 事实1；事实2」，
            # 后半段与 key_facts 重复，正文只取第一段
            summary = raw_summary.split(" | ", 1)[0].strip()
            if not summary:
                continue
            # 同一段对话可能被切出多条摘要，去掉正文互为前缀的重复项
            if any(
                summary.startswith(prev.summary[:60])
                or prev.summary.startswith(summary[:60])
                for prev in items
            ):
                continue
            facts = [
                str(f).strip()
                for f in (meta.get("key_facts") or [])
                if str(f).strip() and str(f).strip()[:20] not in summary
            ]
            created = self._parse_dt(row["created_at"]) or datetime.now()
            items.append(
                MemoryItem(
                    doc_id=int(row["id"]),
                    created_at=created,
                    summary=summary[:max_chars].strip(),
                    topics=[str(t).strip() for t in (meta.get("topics") or [])][:4],
                    key_facts=facts[:4],
                    importance=float(meta.get("importance") or 0.0),
                    start=self._parse_dt(meta.get("source_time_start")),
                    end=self._parse_dt(meta.get("source_time_end")),
                )
            )

        # 主题命中优先：topics 里出现 MC 关键词 > key_facts 命中 > 正文顺口提到
        def score(item: MemoryItem) -> float:
            topics_text = " ".join(item.topics)
            facts_text = " ".join(item.key_facts)
            value = 0.0
            for kw in keywords:
                if kw in topics_text:
                    value += 3.0
                if kw in facts_text:
                    value += 2.0
                if kw in item.summary:
                    value += 1.0
            return value

        ranked = sorted(
            items,
            key=lambda x: (score(x), x.doc_id),
            reverse=True,
        )
        return ranked[: max(1, limit)]

    async def fetch(
        self,
        *,
        platform_id: str,
        since_id: int = 0,
        lookback_hours: int | None = None,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        """取最近一段时间内与 MC 相关的记忆，最新的在前。"""
        import asyncio

        return await asyncio.to_thread(
            self._fetch_sync,
            persona=self.persona(platform_id),
            since_id=int(since_id or 0),
            lookback_hours=int(
                lookback_hours
                if lookback_hours is not None
                else (self.memory_cfg.lookback_hours or 48)
            ),
            limit=int(
                limit if limit is not None else (self.memory_cfg.max_items or 3)
            ),
        )

    # ==================== 素材渲染 ====================

    def digest(self, items: list[MemoryItem]) -> str:
        """把记忆渲染成给 LLM 的素材块（按时间正序，便于叙述）。"""
        lines: list[str] = []
        for item in sorted(items, key=lambda x: x.created_at):
            head = f"【{item.time_label}】"
            lines.append(f"{head}{item.summary}")
            facts = [f for f in item.key_facts if f]
            if facts:
                lines.append("  · 细节：" + "；".join(facts))
        return "\n".join(lines)

    # ==================== 配图 ====================

    def pick_screenshot(self, items: list[MemoryItem]) -> str | None:
        """
        为记忆挑一张时间吻合的 MC 实况截图。

        规则：截图 mtime 落在记忆的 source_time 窗口 ±tolerance 分钟内，
        多张命中时取最接近窗口结束时刻的那张；没有吻合的就不配图。
        """
        if not self.memory_cfg.attach_screenshot:
            return None

        directory = self.screenshot_dir()
        if not directory.is_dir():
            logger.debug(f"[memory] 截图目录不存在，跳过配图：{directory}")
            return None

        tolerance = max(0, int(self.memory_cfg.screenshot_tolerance_minutes or 10))
        tolerance_s = tolerance * 60

        try:
            shots: list[tuple[float, Path]] = [
                (p.stat().st_mtime, p)
                for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
            ]
        except OSError as e:
            logger.warning(f"[memory] 枚举截图目录失败：{e}")
            return None
        if not shots:
            return None

        for item in sorted(items, key=lambda x: x.created_at, reverse=True):
            start = item.start or item.created_at
            end = item.end or item.created_at
            lo = start.timestamp() - tolerance_s
            hi = end.timestamp() + tolerance_s
            hits = [(ts, p) for ts, p in shots if lo <= ts <= hi]
            if not hits:
                continue
            # 同一事件会连拍多张，取离事件结束最近的一张
            ts, path = min(hits, key=lambda x: abs(x[0] - end.timestamp()))
            logger.info(
                f"[memory] 配图命中记忆 #{item.doc_id}：{path.name}"
                f"（记忆窗口 {start:%m-%d %H:%M:%S} ~ {end:%H:%M:%S}）"
            )
            return str(path)
        return None
