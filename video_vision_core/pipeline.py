"""解析流水线：把一条「带视频的消息」变成一句可注入的摘要。

时序（越靠前越便宜，任何一步能提前结束就提前结束）：

    ① 认出视频标记                   0 成本
    ② meta 缓存键命中 → 直接返回      0 下载 / 0 token
    ③ 回查 get_msg 拿 url            一次本地 RPC
    ④ 预算闸门                        0 成本，超限即放弃
    ⑤ 下载（体积上限，流式）           带宽成本
    ⑥ content 缓存键命中 → 返回       0 token
    ⑦ ffmpeg 抽帧 + dHash 去重        CPU 成本
    ⑧ 一次视觉请求                    唯一的 token 支出
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import ffmpeg as ffmpeg_tools
from .cache import CacheStore, content_key, meta_key
from .detect import (
    compile_markers,
    find_marker_indices,
    guess_extension,
    iter_segments,
    parse_marker_hint,
    segment_text,
)
from .frames import dedupe_frames, plan_timestamps, to_base64
from .source import SourceError, fetch_video_bytes, resolve_source
from .vision import VisionError, describe_frames


@dataclass
class AnalyzeResult:
    """一次视频解析的结果。"""

    ok: bool
    caption: str = ""
    frames: int = 0
    cached: bool = False
    deferred: bool = False
    reason: str = ""
    error: str = ""
    tokens: int = 0
    elapsed_ms: int = 0
    duration: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class VideoPipeline:
    """视频解析流水线，配置热更新后重建即可。"""

    def __init__(
        self,
        ctx: Any,
        cache: CacheStore,
        config_provider: Callable[[], Any],
        logger: Any = None,
    ) -> None:
        """初始化流水线。

        Args:
            ctx: 插件上下文。
            cache: 缓存实例。
            config_provider: 返回最新配置对象的可调用对象。
            logger: 日志器。
        """

        self._ctx = ctx
        self._cache = cache
        self._config_provider = config_provider
        self._logger = logger
        self._semaphore = asyncio.Semaphore(max(1, int(config_provider().budget.max_concurrent)))

    # ---------------- 对外入口 ----------------

    async def analyze_message(
        self,
        message: Mapping[str, Any],
        *,
        question: str = "",
        cache_only: bool = False,
        budget_ms: int | None = None,
    ) -> AnalyzeResult:
        """解析一条消息里的视频。

        Args:
            message: 宿主消息字典。
            question: 需要模型回答的问题；空串表示只要摘要。
            cache_only: 只查缓存，未命中返回 ``deferred=True``（钩子里省时间用）。
            budget_ms: 本次调用的总耗时上限（毫秒）；None 表示用配置里的钩子预算。

        Returns:
            AnalyzeResult: 解析结果。
        """

        started = time.monotonic()
        config = self._config_provider()
        budget = int(config.budget.hook_sync_budget_ms if budget_ms is None else budget_ms)
        timeout = None if budget <= 0 else budget / 1000.0

        if timeout is not None:
            try:
                return await asyncio.wait_for(self._analyze_inner(message, question, cache_only), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                await self._cache.record_error()
                self._log("warning", "视频解析超时（%sms），本次不注入摘要", budget)
                return AnalyzeResult(
                    ok=False,
                    deferred=True,
                    reason="timeout",
                    error=f"解析超过 {budget}ms",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

        return await self._analyze_inner(message, question, cache_only)

    async def analyze_recent_video(self, chat_id: str, *, question: str = "") -> AnalyzeResult:
        """在指定聊天流里找最近一条带视频的消息并解析（给 watch_video 工具用）。

        Args:
            chat_id: 聊天流 ID。
            question: 需要回答的问题。

        Returns:
            AnalyzeResult: 解析结果；找不到视频时返回带原因的失败结果。
        """

        config = self._config_provider()
        limit = max(3, int(config.detect.scan_recent_for_tool))
        try:
            history = await self._ctx.message.get_recent(chat_id, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 历史查不到不是致命错误
            return AnalyzeResult(ok=False, reason="history_failed", error=f"读取历史消息失败: {exc}")

        markers = compile_markers(config.detect.marker_patterns)
        for item in reversed(list(history or [])):
            candidate = self._as_message_dict(item)
            if not candidate:
                continue
            if find_marker_indices(candidate, markers):
                return await self.analyze_message(candidate, question=question, budget_ms=90000)

        return AnalyzeResult(ok=False, reason="no_video", error="最近的聊天记录里没有找到视频消息")

    # ---------------- 内部实现 ----------------

    @staticmethod
    def _as_message_dict(item: Any) -> dict[str, Any] | None:
        """把历史消息对象/字典统一成字典。

        Args:
            item: 历史消息项。

        Returns:
            dict | None: 消息字典；无法转换时返回 None。
        """

        if isinstance(item, Mapping):
            if isinstance(item.get("raw_message"), list):
                return dict(item)
            nested = item.get("message")
            if isinstance(nested, Mapping) and isinstance(nested.get("raw_message"), list):
                return dict(nested)
            return dict(item) if "message_id" in item else None
        return None

    def _log(self, level: str, message: str, *args: Any) -> None:
        """安全地写日志。

        Args:
            level: 日志级别。
            message: 格式化模板。
            *args: 模板参数。
        """

        if self._logger is None:
            return
        try:
            getattr(self._logger, level)(message, *args)
        except Exception:  # noqa: BLE001 - 日志永远不该影响主逻辑
            pass

    def _collect_hint(self, message: Mapping[str, Any], markers: Any) -> dict[str, Any]:
        """从占位文本里收集文件名/大小，作为廉价缓存键原料。

        Args:
            message: 消息字典。
            markers: 已编译的正则。

        Returns:
            dict: 可能包含 ``file`` / ``size``。
        """

        for segment in iter_segments(message):
            text = segment_text(segment)
            if text and any(pattern.search(text) for pattern in markers):
                hint = parse_marker_hint(text)
                if hint:
                    return hint
        return {}

    async def _analyze_inner(
        self,
        message: Mapping[str, Any],
        question: str,
        cache_only: bool,
    ) -> AnalyzeResult:
        """真正的解析流程（可被外层包超时）。

        Args:
            message: 消息字典。
            question: 提问内容。
            cache_only: 是否只查缓存。

        Returns:
            AnalyzeResult: 结果。
        """

        started = time.monotonic()
        config = self._config_provider()
        markers = compile_markers(config.detect.marker_patterns)
        hint = self._collect_hint(message, markers)

        source = None
        cheap_keys = [meta_key(str(hint.get("file") or ""), int(hint.get("size") or 0))]
        cached = await self._cache.get(cheap_keys)
        if cached:
            return self._cached_result(cached, started)

        if cache_only:
            return AnalyzeResult(
                ok=False,
                deferred=True,
                reason="cache_only",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            source = await resolve_source(self._ctx, message, self._logger)
        except SourceError as exc:
            await self._cache.record_error()
            self._log("warning", "视频插件: 视频源解析失败 [%s] %s", exc.reason, exc.detail)
            return AnalyzeResult(
                ok=False,
                reason=exc.reason,
                error=exc.detail or str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        keys = list(cheap_keys)
        if source.file_name or source.size_hint:
            keys.append(meta_key(source.file_name, source.size_hint))
        cached = await self._cache.get(keys)
        if cached:
            return self._cached_result(cached, started)

        allowed, block_reason = await self._cache.reserve_slot()
        if not allowed:
            return AnalyzeResult(
                ok=False,
                reason="budget",
                error=block_reason,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        max_bytes = max(1, int(config.budget.max_download_mb)) * 1024 * 1024
        try:
            data = await fetch_video_bytes(
                self._ctx,
                source,
                max_bytes=max_bytes,
                timeout=float(config.runtime.download_timeout_seconds),
                security=config.security,
                logger=self._logger,
            )
        except SourceError as exc:
            await self._cache.record_error()
            self._log("warning", "视频插件: 视频下载失败 [%s] %s", exc.reason, exc.detail)
            return AnalyzeResult(
                ok=False,
                reason=exc.reason,
                error=exc.detail or str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        exact_key = content_key(data)
        cached = await self._cache.get([exact_key])
        if cached:
            await self._cache.put(keys, cached)
            await self._cache.save()
            return self._cached_result(cached, started)

        async with self._semaphore:
            result = await self._run_local_pipeline(data, source, config, question, started)

        if result.ok:
            await self._cache.put([*keys, exact_key], {
                "caption": result.caption,
                "frames": result.frames,
                "duration": result.duration,
                "tokens": result.tokens,
                "model": result.meta.get("model", ""),
                "file": source.file_name,
                "size": source.size_hint,
            })
            await self._cache.charge(result.tokens)
            await self._cache.save()
        else:
            await self._cache.record_error()

        return result

    async def _run_local_pipeline(
        self,
        data: bytes,
        source: Any,
        config: Any,
        question: str,
        started: float,
    ) -> AnalyzeResult:
        """下载完成后的本地处理：落盘 → 探测 → 抽帧 → 去重 → 视觉。

        Args:
            data: 视频字节。
            source: 视频来源。
            config: 当前配置。
            question: 提问。
            started: 起始单调时钟。

        Returns:
            AnalyzeResult: 结果。
        """

        runtime_dir = Path(getattr(self._ctx.paths, "runtime_dir", Path("temp") / "plugins"))
        work_dir = runtime_dir / f"video-vision-{uuid.uuid4().hex[:12]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        video_path = work_dir / f"input{guess_extension(source.data)}"

        try:
            await asyncio.to_thread(video_path.write_bytes, data)

            ffmpeg_exe = ffmpeg_tools.find_ffmpeg(config.runtime.ffmpeg_path, self._logger)
            if not ffmpeg_exe:
                return self._fail(
                    "no_ffmpeg",
                    "宿主没有可用的 ffmpeg（可配置 ffmpeg_path，或安装 imageio-ffmpeg）",
                    started,
                )

            try:
                info = await ffmpeg_tools.probe(ffmpeg_exe, video_path)
            except ffmpeg_tools.FFmpegError as exc:
                return self._fail("probe_failed", str(exc), started)

            if not info.has_video:
                return self._fail("no_video_stream", "这个文件里没有视频轨（可能只是语音或图片）", started)

            duration = info.duration if info.duration > 0 else 0.0
            effective_duration = min(duration, float(config.budget.max_duration_seconds)) if duration else duration

            timestamps = plan_timestamps(
                effective_duration,
                min_frames=config.frames.min_frames,
                max_frames=config.frames.max_frames,
                seconds_per_frame=config.frames.seconds_per_frame,
            )

            frame_paths: list[Path] = []
            for index, stamp in enumerate(timestamps):
                target = work_dir / f"frame-{index:02d}.jpg"
                produced = await ffmpeg_tools.extract_frame_robust(
                    ffmpeg_exe,
                    video_path,
                    stamp,
                    target,
                    max_edge=config.frames.max_edge_px,
                    quality=config.frames.jpeg_quality,
                )
                if produced is not None:
                    frame_paths.append(produced)

            if not frame_paths:
                return self._fail("no_frame", "抽帧失败（视频可能已损坏或编码不受支持）", started)

            if config.frames.dedupe:
                frame_paths = dedupe_frames(frame_paths, threshold=config.frames.dedupe_threshold)

            images = [to_base64(path) for path in frame_paths]
            try:
                vision = await describe_frames(
                    self._ctx,
                    images,
                    task_name=config.vision.task_name,
                    model_name=config.vision.model_name,
                    temperature=config.vision.temperature,
                    max_tokens=config.vision.max_output_tokens,
                    duration=effective_duration,
                    max_chars=config.vision.caption_max_chars,
                    question=question,
                    ask_max_chars=config.runtime.ask_max_chars,
                )
            except VisionError as exc:
                return self._fail("vision_failed", str(exc), started)

            elapsed = int((time.monotonic() - started) * 1000)
            self._log(
                "info",
                "视频插件: 解析完成 帧=%s token≈%s 用时=%sms 模型=%s",
                len(images),
                vision.tokens_estimate,
                elapsed,
                vision.model,
            )
            return AnalyzeResult(
                ok=True,
                caption=vision.text,
                frames=len(images),
                tokens=vision.tokens_estimate,
                elapsed_ms=elapsed,
                duration=effective_duration,
                meta={
                    "model": vision.model,
                    "resolution": f"{info.width}x{info.height}",
                    "truncated": bool(duration and duration > effective_duration),
                },
            )
        finally:
            if not getattr(config.budget, "debug_keep_frames", False):
                await asyncio.to_thread(shutil.rmtree, work_dir, True)
            else:
                self._log("info", "视频插件: 调试模式保留临时目录 %s", work_dir)

    @staticmethod
    def _cached_result(record: Mapping[str, Any], started: float) -> AnalyzeResult:
        """把缓存记录还原成结果对象。

        Args:
            record: 缓存记录。
            started: 起始单调时钟。

        Returns:
            AnalyzeResult: 命中缓存的结果（tokens=0）。
        """

        return AnalyzeResult(
            ok=True,
            caption=str(record.get("caption") or ""),
            frames=int(record.get("frames") or 0),
            cached=True,
            tokens=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            duration=float(record.get("duration") or 0.0),
            meta={"model": record.get("model", ""), "from_cache": True},
        )

    @staticmethod
    def _fail(reason: str, error: str, started: float) -> AnalyzeResult:
        """构造失败结果。

        Args:
            reason: 原因码。
            error: 错误详情。
            started: 起始单调时钟。

        Returns:
            AnalyzeResult: 失败结果。
        """

        return AnalyzeResult(
            ok=False,
            reason=reason,
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
