"""视频识眼 · MaiBot 插件入口。

接入方式（两条路径互补）：

1. `chat.receive.before_process` 钩子：在消息进入推理前，把视频「翻译」成一句
   摘要，以文本段插在 `[视频]` 占位符后面 —— 聊天模型于是「看见」了视频，
   而且**不需要额外一次 LLM 请求**。缓存命中时这一步是 0 token。
2. `watch_video` 工具：群聊没被 @、或用户追问「视频里那行字写了啥」时，
   由模型按需调用，避免每条视频都自动解析。

命令：`/video status`、`/video clear`（操作员级）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

# 保证同目录下的 video_vision_core 包一定可导入：不同版本的 Runner 对
# 「插件目录是否进 sys.path」处理不完全一致，这里显式兜一层。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool  # noqa: E402
from maibot_sdk.types import (  # noqa: E402
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from video_vision_core.cache import CacheStore  # noqa: E402
from video_vision_core.config_model import VideoVisionConfig  # noqa: E402
from video_vision_core.detect import (  # noqa: E402
    compile_markers,
    find_marker_indices,
    is_addressed,
    is_private_chat,
)
from video_vision_core.ffmpeg import find_ffmpeg  # noqa: E402
from video_vision_core.pipeline import AnalyzeResult, VideoPipeline  # noqa: E402

PLUGIN_ID = "github.saltinanon.video-vision"


class VideoVisionPlugin(MaiBotPlugin):
    """让麦麦看见视频。"""

    config_model = VideoVisionConfig

    def __init__(self) -> None:
        """初始化插件（此时还没有 ctx，真正的初始化在 on_load）。"""

        self._cache: CacheStore | None = None
        self._pipeline: VideoPipeline | None = None
        self._markers: tuple[Any, ...] = ()
        self._cooldowns: dict[str, float] = {}

    # ---------------- 生命周期 ----------------

    async def on_load(self) -> None:
        """注入上下文后构建缓存与流水线。"""

        await self._rebuild()
        config = self._safe_config()
        ffmpeg_path = find_ffmpeg(config.runtime.ffmpeg_path, self.ctx.logger)
        if ffmpeg_path:
            self.ctx.logger.info("视频识眼已加载：ffmpeg=%s，钩子预算=%sms", ffmpeg_path, config.budget.hook_sync_budget_ms)
        else:
            self.ctx.logger.warning("视频识眼已加载，但没有可用的 ffmpeg：视频解析会返回明确错误而不是静默降级")

    async def on_unload(self) -> None:
        """卸载时把缓存落盘。"""

        if self._cache is not None:
            await self._cache.save()
        self._pipeline = None
        self._cache = None
        self.ctx.logger.info("视频识眼已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新：重建缓存与流水线。

        Args:
            scope: 配置作用域。
            config_data: 新配置数据。
            version: 配置版本。
        """

        self.ctx.logger.info("视频识眼配置更新：scope=%s version=%s", scope, version)
        if scope in ("self", ""):
            await self._rebuild()

    # ---------------- 内部工具 ----------------

    def _safe_config(self) -> VideoVisionConfig:
        """读取强类型配置，必要时用原始字典兜底。

        Returns:
            VideoVisionConfig: 配置实例。
        """

        try:
            return self.config  # type: ignore[return-value]
        except Exception:
            self.set_plugin_config(self.get_plugin_config_data())
            return self.config  # type: ignore[return-value]

    def _data_dir(self) -> Path:
        """返回插件持久化目录。

        Returns:
            Path: 数据目录。
        """

        paths = getattr(self.ctx, "paths", None)
        if paths is not None:
            return Path(paths.data_dir)
        return Path("data") / "plugins" / PLUGIN_ID

    async def _rebuild(self) -> None:
        """按最新配置重建缓存与流水线。"""

        config = self._safe_config()
        cache = CacheStore(
            self._data_dir() / "video_cache.json",
            ttl_hours=config.budget.cache_ttl_hours,
            daily_video_limit=config.budget.daily_video_limit,
            daily_token_budget=config.budget.daily_token_budget,
            logger=self.ctx.logger,
        )
        await cache.load()
        self._cache = cache
        self._markers = compile_markers(config.detect.marker_patterns)
        # 每次配置变更都重建流水线：信号量、缓存参数都跟着新配置走
        self._pipeline = VideoPipeline(self.ctx, cache, self._safe_config, self.ctx.logger)

    def _build_note(self, result: AnalyzeResult, config: VideoVisionConfig) -> str:
        """把解析结果拼成注入文本。

        Args:
            result: 解析结果。
            config: 当前配置。

        Returns:
            str: 注入到消息里的文本。
        """

        prefix = (config.detect.inject_prefix or "[视频内容]").strip()
        caption = result.caption.strip()
        if not caption:
            return ""
        if not config.detect.include_meta:
            return f"{prefix} {caption}"
        meta_parts: list[str] = [f"{result.frames}帧"]
        if result.duration > 0:
            meta_parts.append(f"{result.duration:.0f}s")
        if result.cached:
            meta_parts.append("缓存")
        return f"{prefix} {caption}（{'/'.join(meta_parts)}）"

    def _inject(self, message: dict[str, Any], note: str) -> bool:
        """把摘要插到最后一个视频标记段之后。

        Args:
            message: 宿主消息字典（原地修改）。
            note: 要插入的文本。

        Returns:
            bool: 是否成功插入。
        """

        raw = message.get("raw_message")
        if not isinstance(raw, list):
            return False
        indices = find_marker_indices(message, self._markers)
        insert_at = (indices[-1] + 1) if indices else len(raw)
        raw.insert(insert_at, {"type": "text", "data": f" {note}"})
        message["raw_message"] = raw

        existing = message.get("processed_plain_text")
        if isinstance(existing, str) and existing.strip():
            message["processed_plain_text"] = f"{existing} {note}"
        return True

    def _cooldown_ok(self, message: dict[str, Any], seconds: int) -> bool:
        """检查聊天流冷却。

        Args:
            message: 消息字典。
            seconds: 冷却秒数。

        Returns:
            bool: 允许解析时为 True。
        """

        if seconds <= 0:
            return True
        chat_key = str(message.get("session_id") or message.get("message_id") or "unknown")
        now = time.monotonic()
        last = self._cooldowns.get(chat_key, 0.0)
        if now - last < seconds:
            return False
        self._cooldowns[chat_key] = now
        return True

    # ---------------- 接入点 1：入站钩子 ----------------

    @HookHandler(
        "chat.receive.before_process",
        name="video_vision_inject",
        description="识别入站视频消息，抽帧交给视觉模型，把摘要注入消息体",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=30000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_incoming(self, **kwargs: Any) -> dict[str, Any]:
        """在消息进入主链前补上视频内容描述。

        Args:
            **kwargs: 钩子载荷，其中 ``message`` 为序列化后的 SessionMessage。

        Returns:
            dict: Hook 控制指令。
        """

        message = kwargs.get("message")
        if not isinstance(message, dict) or self._pipeline is None:
            return {"action": "continue", "modified_kwargs": kwargs}

        try:
            note = await self._describe_for_hook(message)
            if note and self._inject(message, note):
                kwargs["message"] = message
                self.ctx.logger.info("视频识眼: 已注入视频摘要 %s", note[:60])
        except Exception as exc:  # noqa: BLE001 - 钩子绝不能打断消息主链
            self.ctx.logger.warning("视频识眼: 钩子处理异常，已放行原消息: %s", exc, exc_info=True)

        return {"action": "continue", "modified_kwargs": kwargs}

    async def _describe_for_hook(self, message: dict[str, Any]) -> str:
        """钩子内的解析决策，返回要注入的文本（空串表示不注入）。

        Args:
            message: 消息字典。

        Returns:
            str: 注入文本。
        """

        config = self._safe_config()
        if config.detect.trigger_mode == "off":
            return ""

        if not find_marker_indices(message, self._markers):
            return ""

        if config.detect.trigger_mode == "mention_or_private":
            if not is_private_chat(message) and not is_addressed(message):
                return ""

        budget_ms = int(config.budget.hook_sync_budget_ms)
        cooling = not self._cooldown_ok(message, int(config.detect.cooldown_seconds))
        # 预算为 0 或处于冷却期时，只允许 0 成本的缓存命中
        cache_only = budget_ms <= 0 or cooling

        result = await self._pipeline.analyze_message(  # type: ignore[union-attr]
            message,
            cache_only=cache_only,
            budget_ms=max(budget_ms, 5000) if not cache_only else 0,
        )

        if result.ok:
            return self._build_note(result, config)

        if result.deferred:
            self.ctx.logger.debug("视频识眼: 本次跳过（%s）", result.reason)
            return ""
        self.ctx.logger.warning("视频识眼: 解析失败（%s）：%s", result.reason, result.error)
        if config.detect.inject_on_failure and result.error:
            return f"{config.detect.inject_prefix}（未解析成功：{result.error[:60]}）"
        return ""

    # ---------------- 接入点 2：模型工具 ----------------

    @Tool(
        "watch_video",
        brief_description="查看当前聊天里最近一个视频的内容（画面描述或回答问题）",
        detailed_description=(
            "当聊天里出现过视频、而你需要知道视频画面里到底是什么时调用。"
            "只解析最近一条带视频的消息；同一个视频命过缓存时不会重复消耗视觉模型。"
            "如果视频内容已经在上下文里（形如 [视频内容] …），就不要再调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=False,
            ),
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="想从视频画面里确认的问题；留空则返回整体描述",
                required=False,
            ),
        ],
    )
    async def watch_video(self, stream_id: str = "", question: str = "", **kwargs: Any) -> dict[str, Any]:
        """按需解析最近的视频消息。

        Args:
            stream_id: 聊天流 ID。
            question: 具体问题。
            **kwargs: 额外参数（宿主可能直接注入 stream_id）。

        Returns:
            dict: 工具结果，其中 ``content`` 为给模型看的文本。
        """

        chat_id = str(stream_id or kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not chat_id:
            return {"success": False, "name": "watch_video", "content": "缺少 stream_id，无法定位聊天流。"}
        if self._pipeline is None:
            return {"success": False, "name": "watch_video", "content": "插件尚未完成初始化。"}

        result = await self._pipeline.analyze_recent_video(chat_id, question=str(question or "").strip())
        if result.ok:
            self.ctx.logger.info(
                "视频识眼: 工具解析完成 cached=%s 帧=%s token≈%s",
                result.cached,
                result.frames,
                result.tokens,
            )
            return {
                "success": True,
                "name": "watch_video",
                "content": result.caption,
                "cached": result.cached,
                "frames": result.frames,
            }
        return {
            "success": False,
            "name": "watch_video",
            "content": f"没能看到视频：{result.error or result.reason}",
        }

    # ---------------- 接入点 3：管理命令 ----------------

    @Command(
        "video",
        description="视频识眼：查看运行状态或清理缓存",
        pattern=r"^/video(?:\s+(?P<action>status|clear|help))?$",
        permission="operator",
    )
    async def cmd_video(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /video 命令。

        Args:
            **kwargs: 命令上下文。

        Returns:
            tuple[bool, str, int]: (成功, 回复文本, 权重)。
        """

        action = str((kwargs.get("matched_groups") or {}).get("action") or "help").lower()
        config = self._safe_config()

        if action == "clear":
            removed = await self._cache.clear() if self._cache else 0
            return True, f"已清空 {removed} 条视频摘要缓存。", 1

        if action == "status":
            return True, await self._status_text(config), 1

        return (
            True,
            "视频识眼：\n"
            "/video status — 查看 ffmpeg、缓存与今日用量\n"
            "/video clear — 清空摘要缓存\n"
            "发视频时自动解析（私聊/被@），也可让模型调用 watch_video。",
            1,
        )

    async def _status_text(self, config: VideoVisionConfig) -> str:
        """拼装状态文本。

        Args:
            config: 当前配置。

        Returns:
            str: 多行状态。
        """

        stats = await self._cache.snapshot() if self._cache else {}
        ffmpeg_path = find_ffmpeg(config.runtime.ffmpeg_path)
        tokens_left = stats.get("tokens_left", -1)
        videos_left = stats.get("videos_left", -1)
        return "\n".join(
            [
                "视频识眼状态",
                f"ffmpeg：{ffmpeg_path or '未找到'}",
                f"触发模式：{config.detect.trigger_mode}｜冷却 {config.detect.cooldown_seconds}s｜钩子预算 {config.budget.hook_sync_budget_ms}ms",
                f"抽帧：{config.frames.min_frames}-{config.frames.max_frames} 帧｜长边 {config.frames.max_edge_px}px｜去重 {'开' if config.frames.dedupe else '关'}",
                f"今日：{stats.get('videos_today', 0)} 个视频 / 约 {stats.get('tokens_today', 0)} token"
                f"（剩余 {videos_left if videos_left >= 0 else '不限'} 个 / {tokens_left if tokens_left >= 0 else '不限'} token）",
                f"缓存：{stats.get('entries', 0)} 条｜命中 {stats.get('hits', 0)}｜未命中 {stats.get('misses', 0)}｜失败 {stats.get('errors', 0)}",
            ]
        )


def create_plugin() -> VideoVisionPlugin:
    """SDK 约定的工厂函数。

    Returns:
        VideoVisionPlugin: 插件实例。
    """

    return VideoVisionPlugin()
