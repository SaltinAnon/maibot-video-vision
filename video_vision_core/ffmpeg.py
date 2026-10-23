"""ffmpeg 封装：定位二进制、读元信息、按时间点抽帧。

为什么用 ffmpeg 而不是 OpenCV：
- OpenCV 的 wheel 一个就 60MB+，还要 numpy 版本对齐；
- ffmpeg 是本机常见工具，缺了还能用 imageio-ffmpeg 自带的静态二进制兜底；
- 抽帧是一次性 CPU 开销，不进 token 账本。

约定：所有阻塞调用都丢进 `asyncio.to_thread`，绝不阻塞 Runner 的事件循环。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_SIZE_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_ROTATE_RE = re.compile(r"rotate\s*:\s*(-?\d+)")

_resolved_cache: dict[str, str | None] = {}


class FFmpegError(Exception):
    """ffmpeg 不可用或执行失败。"""


@dataclass(frozen=True)
class MediaInfo:
    """视频元信息。"""

    duration: float
    width: int
    height: int
    has_video: bool


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """同步执行子进程，统一隐藏 Windows 控制台窗口。

    Args:
        args: 命令行参数列表。
        timeout: 超时秒数。

    Returns:
        CompletedProcess: 执行结果（不抛非零退出）。
    """

    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _from_imageio() -> str | None:
    """尝试用 imageio-ffmpeg 自带的静态二进制。

    Returns:
        str | None: 可执行文件路径；未安装时返回 None。
    """

    try:
        import imageio_ffmpeg  # 延迟导入：没装也不影响系统 ffmpeg 路径

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and Path(exe).is_file() else None
    except Exception:  # noqa: BLE001 - 依赖缺失、下载失败都只是降级
        return None


def find_ffmpeg(preferred: str = "", logger: Any = None) -> str | None:
    """按优先级定位 ffmpeg 可执行文件，结果进程内缓存。

    顺序：配置路径 → 环境变量 → PATH → imageio-ffmpeg 自带。

    Args:
        preferred: 配置里指定的路径。
        logger: 可选日志器。

    Returns:
        str | None: 找到的路径；全部失败返回 None。
    """

    cache_key = preferred or ""
    if cache_key in _resolved_cache:
        return _resolved_cache[cache_key]

    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    for env_name in ("FFMPEG_BINARY", "IMAGEIO_FFMPEG_EXE", "FFMPEG_PATH"):
        env_value = os.environ.get(env_name)
        if env_value:
            candidates.append(env_value)
    which_value = shutil.which("ffmpeg")
    if which_value:
        candidates.append(which_value)
    imageio_value = _from_imageio()
    if imageio_value:
        candidates.append(imageio_value)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            _resolved_cache[cache_key] = str(path)
            if logger is not None:
                logger.info("视频插件: 使用 ffmpeg -> %s", path)
            return str(path)

    _resolved_cache[cache_key] = None
    if logger is not None:
        logger.warning("视频插件: 未找到 ffmpeg，视频解析不可用（可配置 ffmpeg_path 或安装 imageio-ffmpeg）")
    return None


def reset_ffmpeg_cache() -> None:
    """清空定位缓存（配置热更新后调用）。"""

    _resolved_cache.clear()


def _probe_sync(ffmpeg: str, path: Path, timeout: float) -> MediaInfo:
    """同步读取媒体信息。

    Args:
        ffmpeg: ffmpeg 路径。
        path: 视频文件。
        timeout: 超时秒数。

    Returns:
        MediaInfo: 解析出的元信息。

    Raises:
        FFmpegError: 完全读不出信息时抛出。
    """

    process = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], timeout)
    stderr = process.stderr or ""

    duration = 0.0
    match = _DURATION_RE.search(stderr)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    width = height = 0
    size_match = _VIDEO_SIZE_RE.search(stderr)
    if size_match:
        width, height = int(size_match.group(1)), int(size_match.group(2))

    has_video = "Video:" in stderr and width > 0 and height > 0
    if not has_video and duration <= 0:
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else "无输出"
        raise FFmpegError(f"无法解析视频: {tail}")

    return MediaInfo(duration=duration, width=width, height=height, has_video=has_video)


async def probe(ffmpeg: str, path: Path, *, timeout: float = 20.0) -> MediaInfo:
    """异步读取视频时长/分辨率。

    Args:
        ffmpeg: ffmpeg 路径。
        path: 视频文件。
        timeout: 超时秒数。

    Returns:
        MediaInfo: 元信息。
    """

    return await asyncio.to_thread(_probe_sync, ffmpeg, path, timeout)


def build_scale_filter(max_edge: int) -> str:
    """构造「长边不超过 max_edge、保持比例、禁止放大」的缩放滤镜。

    Args:
        max_edge: 长边像素上限。

    Returns:
        str: ffmpeg filter 表达式。
    """

    edge = max(64, int(max_edge))
    return (
        f"scale='min({edge},iw)':'min({edge},ih)':force_original_aspect_ratio=decrease:flags=bicubic,"
        f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def _extract_sync(ffmpeg: str, source: Path, timestamp: float, target: Path, filter_expr: str, quality: int, timeout: float) -> bool:
    """同步抽一帧。

    Args:
        ffmpeg: ffmpeg 路径。
        source: 视频文件。
        timestamp: 目标时间点（秒）。
        target: 输出 JPEG 路径。
        filter_expr: 缩放滤镜。
        quality: JPEG 质量（2-31）。
        timeout: 超时秒数。

    Returns:
        bool: 是否成功产出非空文件。
    """

    args = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filter_expr,
        "-q:v",
        str(max(2, min(31, quality))),
        "-y",
        str(target),
    ]
    try:
        _run(args, timeout)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return target.is_file() and target.stat().st_size > 0


async def extract_frame(
    ffmpeg: str,
    source: Path,
    timestamp: float,
    target: Path,
    *,
    max_edge: int,
    quality: int,
    timeout: float = 20.0,
) -> bool:
    """异步抽一帧到 JPEG。

    Args:
        ffmpeg: ffmpeg 路径。
        source: 视频文件。
        timestamp: 时间点（秒）。
        target: 输出路径。
        max_edge: 长边上限。
        quality: JPEG 质量。
        timeout: 超时秒数。

    Returns:
        bool: 是否成功。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(
        _extract_sync,
        ffmpeg,
        source,
        timestamp,
        target,
        build_scale_filter(max_edge),
        quality,
        timeout,
    )


async def extract_frame_robust(
    ffmpeg: str,
    source: Path,
    timestamp: float,
    target: Path,
    *,
    max_edge: int,
    quality: int,
    timeout: float = 20.0,
) -> Path | None:
    """抽一帧，失败就往前退一点重试。

    为什么需要重试：h264/h265 的关键帧可能很稀，`-ss` 落在最后一帧之后时
    ffmpeg 会一帧都解不出来（短视频尤其常见）。退 0.6s / 1.5s 通常就能拿到
    内容相近的画面，比整段视频没有摘要好得多。

    Args:
        ffmpeg: ffmpeg 路径。
        source: 视频文件。
        timestamp: 首选时间点（秒）。
        target: 首选输出路径。
        max_edge: 长边上限。
        quality: JPEG 质量。
        timeout: 单次超时秒数。

    Returns:
        Path | None: 成功的帧路径；全部失败返回 None。
    """

    candidates: list[float] = [max(0.0, timestamp)]
    for delta in (0.6, 1.5):
        candidate = max(0.0, timestamp - delta)
        if all(abs(candidate - existing) > 0.01 for existing in candidates):
            candidates.append(candidate)
    candidates.append(0.3)

    for index, candidate in enumerate(candidates):
        output = target if index == 0 else target.with_name(f"{target.stem}-alt{index}{target.suffix}")
        if await extract_frame(
            ffmpeg,
            source,
            candidate,
            output,
            max_edge=max_edge,
            quality=quality,
            timeout=timeout,
        ):
            return output
    return None
