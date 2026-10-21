import json
import os
import random
import re
import zoneinfo
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger

from .config import PluginConfig
from .memory import LivingMemorySource
from .model import Post
from .sender import Sender
from .service import PostService

# 顺延出静默时段时附加的随机抖动上限（秒），避免每次都在静默结束那一秒整点触发
_QUIET_JITTER_SECONDS = 600


def _safe_name(raw: str) -> str:
    """把平台 ID 转成可安全用于文件名的字符串。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "_", str(raw or ""))
    return cleaned or "default"


class BaseTask:
    """
    调度器底座：负责启动 AsyncIOScheduler、任务执行包装与改期，
    具体「下一次什么时候跑」由子类通过 _schedule_after_run() 实现。
    """

    def __init__(self, job_name: str, timezone: zoneinfo.ZoneInfo):
        self.timezone = timezone
        self.job_name = job_name
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()
        self._terminated = False
        # 本轮因夜间免打扰被推迟时，记录改期的绝对时间
        self._pending_defer: datetime | None = None

    def _arm(self, target_time: datetime, suffix: str = "once") -> None:
        """把任务排到指定时刻（一次性），失败只记录不抛出。"""
        if self._terminated:
            return
        try:
            self.scheduler.add_job(
                func=self._run_task_wrapper,
                trigger=DateTrigger(run_date=target_time, timezone=self.timezone),
                name=f"{self.job_name}_{suffix}_{int(target_time.timestamp())}",
                max_instances=1,
            )
        except Exception as e:
            if self._terminated:
                logger.debug(
                    f"[{self.job_name}] 调度器终止后跳过 add_job：{type(e).__name__}: {e}"
                )
                return
            logger.error(f"[{self.job_name}] 添加调度任务失败：{e}")

    async def _run_task_wrapper(self):
        logger.info(f"[{self.job_name}] 开始执行任务")
        try:
            await self.do_task()
        except Exception as e:
            logger.exception(f"[{self.job_name}] 任务执行失败: {e}")
        finally:
            if self._terminated:
                logger.info(f"[{self.job_name}] 本轮任务完成")
                return
            deferred = self._pending_defer
            self._pending_defer = None
            if deferred is not None:
                logger.info(f"[{self.job_name}] 本轮任务改期至 {deferred}")
                self._schedule_deferred(deferred)
            else:
                self._schedule_after_run()
            logger.info(f"[{self.job_name}] 本轮任务完成")

    def _schedule_deferred(self, target_time: datetime):
        """把本轮任务改期到指定时刻执行（夜间免打扰结束后补跑等）。"""
        self._arm(target_time, suffix="deferred")

    def _schedule_after_run(self) -> None:
        raise NotImplementedError

    async def do_task(self):
        raise NotImplementedError

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        self.scheduler.remove_all_jobs()
        try:
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"[{self.job_name}] 关闭调度器时忽略异常：{e}")
        logger.info(f"[{self.job_name}] 已停止")


class AutoRandomCronTask(BaseTask):
    """
    Schedule one task per cron cycle around the cron anchor time.
    Subclasses only need to implement async do_task().
    """

    def __init__(
        self,
        job_name: str,
        cron_expr: str,
        timezone: zoneinfo.ZoneInfo,
        offset_seconds: int,
    ):
        super().__init__(job_name, timezone)

        self.cron_expr = cron_expr
        self.offset_seconds = offset_seconds
        self._last_base_time: datetime | None = None

        self._register_task()

        logger.info(
            f"[{self.job_name}] 已启动，任务周期：{self.cron_expr}，偏移范围：±{self.offset_seconds} 秒"
        )

    def _register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(
                self.cron_expr, timezone=self.timezone
            )
            self._schedule_next_job()
        except Exception as e:
            logger.error(f"[{self.job_name}] Cron 格式错误：{e}")

    def _schedule_next_job(self):
        if self._terminated:
            logger.debug(f"[{self.job_name}] 调度器已终止，跳过后续调度")
            return
        if not hasattr(self, "trigger"):
            return

        now = datetime.now(self.timezone)

        # 计算下一次基准时间：
        # 1) 本轮是「提前」触发（偏移为负，now <= 上次基准时间）时，必须从上次基准时间
        #    之后继续取。若此时直接把 now 传给 get_next_fire_time，APScheduler 3.x 的
        #    CronTrigger 会原样返回同一个基准时间（start = min(now, prev) == prev），
        #    导致同一周期被反复重新排程，配合下面的「立即补偿执行」逻辑会连环发帖。
        # 2) 本轮正常或延迟触发时，直接从当前时刻往后取，避免停机/卡顿后按周期补跑。
        if self._last_base_time is not None and now <= self._last_base_time:
            next_base_time = self.trigger.get_next_fire_time(
                self._last_base_time, self._last_base_time + timedelta(microseconds=1)
            )
        else:
            next_base_time = self.trigger.get_next_fire_time(None, now)

        if not next_base_time:
            logger.error(f"[{self.job_name}] 无法计算下一次基准时间")
            return

        # 兜底：基准时间没有前进说明周期已执行过，直接终止排程，防止同周期重复执行
        if self._last_base_time is not None and next_base_time <= self._last_base_time:
            logger.error(
                f"[{self.job_name}] 下一次基准时间未推进"
                f"（{next_base_time} <= {self._last_base_time}），跳过本轮排程以防重复执行"
            )
            return

        base_time = next_base_time
        self._last_base_time = base_time

        delay_seconds = (
            random.randint(-self.offset_seconds, self.offset_seconds)
            if self.offset_seconds
            else 0
        )
        target_time = base_time + timedelta(seconds=delay_seconds)

        if target_time <= now:
            target_time = now + timedelta(seconds=1)
            logger.warning(
                f"[{self.job_name}] 偏移后时间已过，改为立即补偿执行：{target_time}"
            )

        logger.info(
            f"[{self.job_name}] 基准时间：{base_time}，偏移：{delay_seconds} 秒，执行时间：{target_time}"
        )

        self._arm(target_time)

    def _schedule_after_run(self) -> None:
        self._schedule_next_job()


class AutoComment(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
        platform_id: str = "",
    ):
        cron = config.trigger.comment_cron
        timezone = config.timezone
        offset = config.trigger.comment_offset
        self.platform_id = str(platform_id or "")
        super().__init__(
            f"AutoComment[{self.platform_id or 'default'}]", cron, timezone, offset
        )
        self.cfg = config
        self.service = service
        self.sender = sender

    async def do_task(self):
        # 夜间免打扰：随机偏移有可能把基准时间推进静默时段
        if self.cfg.in_quiet_hours():
            self._defer_past_quiet()
            return
        posts = await self.service.query_feeds(
            pos=0,
            num=20,
            no_self=True,
            no_commented=True,
        )
        for post in posts:
            try:
                await self.service.comment_posts(
                    post, platform_id=self.platform_id
                )
                if self.cfg.trigger.like_when_comment:
                    await self.service.like_posts(post)
                await self.sender.send_admin_post(
                    post,
                    client=self.cfg.resolve_client(self.platform_id),
                    message="定时读说说",
                )
            except Exception as e:
                logger.exception(
                    f"[{self.job_name}] 跳过说说评论失败: tid={post.tid}, uin={post.uin}, name={post.name}, error={e}"
                )

    def _defer_past_quiet(self):
        """命中夜间免打扰时，把本轮改期到静默结束后（带随机抖动）。"""
        quiet = self.cfg.quiet_hours.describe()
        skip_msg = f"[{self.job_name}] 命中夜间免打扰 {quiet}，跳过本轮评论与点赞"
        defer_at = self.cfg.quiet_end_datetime()
        if defer_at is None:
            logger.info(skip_msg)
            return
        jitter = random.randint(0, self.offset_seconds) if self.offset_seconds else 0
        target = defer_at + timedelta(seconds=jitter)
        # 兜底：改期时间若仍落在静默内（例如静默几乎覆盖整天），直接跳过
        if self.cfg.in_quiet_hours(target):
            logger.info(skip_msg)
            return
        self._pending_defer = target
        logger.info(
            f"[{self.job_name}] 命中夜间免打扰 {quiet}，本轮评论与点赞改期至 {target}"
        )


class AutoPublish(BaseTask):
    """
    自动发说说。

    发布时间不再固定钟点，而是「上一次发布之后随机 24~96 小时」（秒级随机），
    更接近真人随手发动态的节奏：

    - 随机出的时刻若落在夜间免打扰时段，会重新抽取，直到落在可发布区间；
    - 冷启动（无历史排程）时以 trigger.publish_cron 作为首次锚点，
      锚点落在静默则顺延到静默结束后（带随机抖动）；
    - 每次排程写入数据目录的 publish_state_<platform>.json，
      重启后沿用未到期的下一次发布时刻，不会因为重载插件而提前发。
    """

    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
        platform_id: str = "",
    ):
        self.platform_id = str(platform_id or "")
        job_name = f"AutoPublish[{self.platform_id or 'default'}]"
        super().__init__(job_name, config.timezone)
        self.cfg = config
        self.service = service
        self.sender = sender
        self.state_path = (
            config.data_dir / f"publish_state_{_safe_name(self.platform_id)}.json"
        )
        self.memory_state_path = (
            config.data_dir / f"memory_state_{_safe_name(self.platform_id)}.json"
        )
        self.memory = LivingMemorySource(config)
        lo_h, hi_h = self._interval_range()
        logger.info(
            f"[{job_name}] 已启动，发布间隔：{lo_h}~{hi_h} 小时（秒级随机），"
            f"夜间免打扰：{self.cfg.quiet_hours.describe()}，"
            f"记忆素材：{'开启' if config.memory.enable else '关闭'}"
        )
        self._bootstrap()

    # ==================== 间隔与排程 ====================

    def _interval_range(self) -> tuple[int, int]:
        """读取发布间隔区间（小时）；max<=0 视为关闭自动发说说。"""
        try:
            lo = int(self.cfg.trigger.publish_interval_min_hours or 0)
            hi = int(self.cfg.trigger.publish_interval_max_hours or 0)
        except (TypeError, ValueError):
            return (24, 96)
        if hi <= 0:
            return (0, 0)
        lo = max(1, lo)
        return (lo, max(lo, hi))

    def _next_random(self, base: datetime) -> datetime:
        """base 之后随机 24~96 小时（精确到秒），并避开夜间免打扰时段。"""
        lo_h, hi_h = self._interval_range()
        if hi_h <= 0:
            return base + timedelta(hours=24)
        lo, hi = lo_h * 3600, hi_h * 3600
        quiet = self.cfg.quiet_hours.describe()
        for _ in range(40):
            target = base + timedelta(seconds=random.randint(lo, hi))
            if not self.cfg.in_quiet_hours(target):
                return target
        # 兜底：静默几乎覆盖整天（或间隔被钉死在同一钟点）时，退化为静默结束 + 抖动
        end = self.cfg.quiet_end_datetime(base)
        if end is not None:
            target = end + timedelta(seconds=random.randint(0, _QUIET_JITTER_SECONDS))
            logger.warning(
                f"[{self.job_name}] 随机 40 次都落在夜间免打扰 {quiet} 内，退化为静默结束时刻：{target}"
            )
            return target
        return base + timedelta(seconds=lo)

    def _defer_out_of_quiet(self, target: datetime, base: datetime) -> datetime:
        """目标时刻若落在静默时段，顺延到静默结束 + 随机抖动。"""
        if not self.cfg.in_quiet_hours(target):
            return target
        end = self.cfg.quiet_end_datetime(target)
        if end is None:
            return target
        deferred = end + timedelta(
            seconds=random.randint(0, _QUIET_JITTER_SECONDS)
        )
        if deferred <= base:
            deferred = base + timedelta(seconds=60)
        logger.info(
            f"[{self.job_name}] 目标时刻 {target} 落在夜间免打扰 "
            f"{self.cfg.quiet_hours.describe()}，顺延至 {deferred}"
        )
        return deferred

    def _cron_anchor(self, now: datetime) -> datetime | None:
        """冷启动锚点：用 publish_cron 算出首次发布时刻，留空则返回 None。"""
        expr = str(self.cfg.trigger.publish_cron or "").strip()
        if not expr:
            return None
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=self.timezone)
        except Exception as e:
            logger.warning(f"[{self.job_name}] publish_cron 格式无效，改用随机间隔：{e}")
            return None
        anchor = trigger.get_next_fire_time(None, now)
        if anchor is None:
            return None
        offset = int(self.cfg.trigger.publish_offset or 0)
        if offset:
            anchor += timedelta(seconds=random.randint(-offset, offset))
            if anchor <= now:
                anchor = now + timedelta(seconds=1)
        return anchor

    def _bootstrap(self) -> None:
        now = datetime.now(self.timezone)
        state = self._load_state()
        saved = state.get("next_publish_ts")
        if saved:
            try:
                target = datetime.fromtimestamp(float(saved), self.timezone)
            except (TypeError, ValueError, OSError):
                target = None
            if target is not None and target > now:
                target = self._defer_out_of_quiet(target, now)
                logger.info(f"[{self.job_name}] 沿用未到期的排程：{target}")
                self._arm(target)
                self._save_state(next_ts=target)
                return
            if target is not None:
                logger.info(f"[{self.job_name}] 上次排程 {target} 已过期，重新排程")

        anchor = self._cron_anchor(now)
        if anchor is not None:
            target = self._defer_out_of_quiet(anchor, now)
            logger.info(
                f"[{self.job_name}] 冷启动：cron 锚点 {anchor} → 首次发布 {target}"
            )
        else:
            target = self._next_random(now)
            logger.info(f"[{self.job_name}] 冷启动：无 cron 锚点，首次发布 {target}")

        self._arm(target)
        self._save_state(next_ts=target)

    def _schedule_after_run(self) -> None:
        now = datetime.now(self.timezone)
        target = self._next_random(now)
        logger.info(
            f"[{self.job_name}] 下一次发布：{target}"
            f"（间隔 {(target - now).total_seconds():.0f} 秒）"
        )
        self._arm(target)
        self._save_state(next_ts=target)

    # ==================== 排程状态持久化 ====================

    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"[{self.job_name}] 读取排程状态失败，按无状态处理：{e}")
        return {}

    def _save_state(
        self, next_ts: datetime | None = None, last_ts: datetime | None = None
    ) -> None:
        state = self._load_state()
        state["platform_id"] = self.platform_id
        if next_ts is not None:
            state["next_publish_ts"] = int(next_ts.timestamp())
            state["next_publish_at"] = next_ts.strftime("%Y-%m-%d %H:%M:%S%z")
        if last_ts is not None:
            state["last_publish_ts"] = int(last_ts.timestamp())
            state["last_publish_at"] = last_ts.strftime("%Y-%m-%d %H:%M:%S%z")
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f"[{self.job_name}] 写入排程状态失败：{e}")

    # ==================== 记忆素材 ====================

    def _load_memory_mark(self) -> int:
        """上一次已消费到的记忆文档 id（水位）。"""
        try:
            if self.memory_state_path.exists():
                data = json.loads(self.memory_state_path.read_text(encoding="utf-8"))
                return int(data.get("last_doc_id") or 0)
        except Exception as e:
            logger.debug(f"[{self.job_name}] 读取记忆水位失败，按 0 处理：{e}")
        return 0

    def _save_memory_mark(self, last_doc_id: int) -> None:
        try:
            tmp = self.memory_state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "platform_id": self.platform_id,
                        "last_doc_id": int(last_doc_id),
                        "updated_at": datetime.now(self.timezone).strftime(
                            "%Y-%m-%d %H:%M:%S%z"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.memory_state_path)
        except Exception as e:
            logger.warning(f"[{self.job_name}] 写入记忆水位失败：{e}")

    async def _collect_memories(
        self, *, force: bool = False
    ) -> tuple[str | None, list[str], int]:
        """
        取近期记忆素材：返回 (素材文本, 配图路径列表, 本次水位)。

        素材会和群聊记录一起交给 LLM，不是二选一；
        失败、无素材、未命中概率时返回 (None, [], 0)，本次只用群聊记录。
        force=True 时跳过概率抽签（管理命令手动触发用）。
        """
        if not self.cfg.memory.enable:
            return None, [], 0

        if not force:
            try:
                prob = float(self.cfg.memory.mc_topic_prob)
            except (TypeError, ValueError):
                prob = 1.0
            if random.random() >= max(0.0, min(1.0, prob)):
                logger.info(f"[{self.job_name}] 本次记忆素材未参与，仅用群聊记录")
                return None, [], 0

        try:
            items = await self.memory.fetch(
                platform_id=self.platform_id,
                since_id=self._load_memory_mark(),
            )
        except Exception as e:
            logger.warning(f"[{self.job_name}] 读取记忆素材失败，本次仅用群聊：{e}")
            return None, [], 0

        if not items:
            logger.info(f"[{self.job_name}] 没有新的记忆素材，本次仅用群聊")
            return None, [], 0

        digest = self.memory.digest(items)
        images: list[str] = []
        shot = self.memory.pick_screenshot(items)
        if shot:
            images.append(shot)
        logger.info(
            f"[{self.job_name}] 命中 {len(items)} 条记忆素材"
            f"（最新 #{items[0].doc_id}，配图 {len(images)} 张）"
        )
        logger.debug(f"[{self.job_name}] 记忆素材：\n{digest}")
        return digest, images, max(item.doc_id for item in items)

    # ==================== 任务体 ====================

    async def run_once(
        self, *, force_memory: bool = False
    ) -> tuple[Post | None, str]:
        """
        立即执行一轮「生成 + 发布」，供管理命令手动触发。

        - 不检查夜间免打扰、不改动下次发布排程，只更新 last_publish_ts；
        - 群聊记录始终参与，force_memory=True 只是跳过记忆参与的概率抽签；
        - 返回 (已发布的说说, 说明)；失败时说说为 None，说明里带原因。
        """
        memory, images, memory_mark = await self._collect_memories(force=force_memory)

        now = datetime.now(self.timezone)
        try:
            text = await self.service.llm.generate_post(
                platform_id=self.platform_id,
                memory=memory,
                memory_prompt=self.cfg.memory.post_prompt,
            )
        except Exception as e:
            logger.error(f"[{self.job_name}] 生成内容失败：{e}")
            return None, f"生成内容失败：{e}"

        if not text:
            return None, "生成内容为空"

        try:
            post = await self.service.publish_post(text=text, images=images or None)
        except Exception as e:
            logger.error(f"[{self.job_name}] 发布失败：{e}")
            return None, f"发布失败：{e}"

        # 只有发布成功才推进记忆水位，失败不吞素材
        if memory_mark:
            self._save_memory_mark(memory_mark)
        self._save_state(last_ts=now)

        note = "群聊+记忆" if memory else "仅群聊"
        if images:
            note += f"，配图 {len(images)} 张"
        logger.info(
            f"[{self.job_name}] 手动发布完成（{note}），tid={getattr(post, 'tid', '')}"
        )
        return post, note

    async def do_task(self):
        now = datetime.now(self.timezone)
        # 夜间免打扰：排程层已经避开，这里兜底（例如配置刚改、或排程被延后）
        if self.cfg.in_quiet_hours(now):
            self._defer_past_quiet()
            return

        post, note = await self.run_once()
        if post is None:
            logger.warning(f"[{self.job_name}] 本轮未发布：{note}")
            return
        await self.sender.send_admin_post(
            post,
            client=self.cfg.resolve_client(self.platform_id),
            message=f"定时发说说（{note}）",
        )

    def _defer_past_quiet(self):
        """命中静默时把本轮发布改期到静默结束后（带随机抖动）。"""
        quiet = self.cfg.quiet_hours.describe()
        skip_msg = f"[{self.job_name}] 命中夜间免打扰 {quiet}，跳过本轮发布"
        defer_at = self.cfg.quiet_end_datetime()
        if defer_at is None:
            logger.info(skip_msg)
            return
        target = defer_at + timedelta(
            seconds=random.randint(0, _QUIET_JITTER_SECONDS)
        )
        if self.cfg.in_quiet_hours(target):
            logger.info(skip_msg)
            return
        self._pending_defer = target
        self._save_state(next_ts=target)
        logger.info(
            f"[{self.job_name}] 命中夜间免打扰 {quiet}，本轮发布改期至 {target}"
        )
