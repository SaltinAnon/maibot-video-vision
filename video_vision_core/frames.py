"""抽帧时序规划与近似重复帧剔除。

「省 token」的两把手：
1. **少抽**：按「平均每 N 秒一帧」估算，再夹在 min/max 之间。一个 60 秒的视频
   默认只出 5 帧，而不是 60 帧。
2. **去重**：用 dHash 比较相邻帧，几乎一样的画面（静止镜头、纯色开场）直接丢掉，
   避免把同一张图重复付一遍图片 token。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Iterable


def plan_timestamps(
    duration: float,
    *,
    min_frames: int,
    max_frames: int,
    seconds_per_frame: float,
) -> list[float]:
    """规划要抽帧的时间点。

    策略：掐掉头尾各 6%（开场黑场、结尾logo最没信息量），在中间等距取样。

    Args:
        duration: 视频时长（秒），未知时按 0 处理。
        min_frames: 最少帧数。
        max_frames: 最多帧数。
        seconds_per_frame: 平均每多少秒一帧。

    Returns:
        list[float]: 升序时间点列表，至少 1 个。
    """

    minimum = max(1, int(min_frames))
    maximum = max(minimum, int(max_frames))
    per_frame = max(0.5, float(seconds_per_frame or 0))

    if duration <= 0:
        return [0.0]

    if duration <= 1.5:
        return [duration * 0.4]

    raw_count = max(1, round(duration / per_frame))
    count = max(minimum, min(maximum, raw_count))
    count = min(count, max(1, int(duration // 0.4)))  # 极短视频别硬抽超过可用的帧数

    start = duration * 0.06
    end = duration * 0.9
    if count == 1:
        return [round(duration * 0.5, 3)]

    step = (end - start) / (count - 1)
    stamps = [round(start + step * index, 3) for index in range(count)]
    # 掐掉最后一帧贴边的风险：h264 关键帧稀疏时，seek 到 EOF 附近可能一帧都出不来
    upper = max(0.0, duration - 0.25)
    return [min(stamp, upper) for stamp in stamps]


def _signature(path: Path, size: int = 8, grid: int = 4) -> tuple[int, list[tuple[int, int, int]]] | None:
    """计算一张图的「结构 + 颜色」双指纹。

    只靠 dHash 会翻车：两个纯色场景的横向梯度几乎一样，dHash 会把它们当同一帧。
    所以再叠一层 4×4 的平均颜色网格，颜色差得多就一定判为不同帧。

    Args:
        path: 图片路径。
        size: dHash 边长。
        grid: 颜色网格边长。

    Returns:
        tuple | None: (dHash, 颜色网格)；Pillow 不可用或读取失败时返回 None。
    """

    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 没装 Pillow 就退化为不去重
        return None

    try:
        with Image.open(path) as image:
            gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            pixels = list(gray.getdata())
            colors = list(image.convert("RGB").resize((grid, grid), Image.Resampling.LANCZOS).getdata())
    except Exception:  # noqa: BLE001
        return None

    bits = 0
    index = 0
    for row in range(size):
        offset = row * (size + 1)
        for column in range(size):
            if pixels[offset + column] > pixels[offset + column + 1]:
                bits |= 1 << index
            index += 1
    return bits, [(int(r), int(g), int(b)) for r, g, b in colors]


def _color_distance(left: list[tuple[int, int, int]], right: list[tuple[int, int, int]]) -> float:
    """计算两张图颜色网格的平均通道差。

    Args:
        left: 颜色网格一。
        right: 颜色网格二。

    Returns:
        float: 0-255 的平均绝对差。
    """

    if not left or not right or len(left) != len(right):
        return 255.0
    total = 0
    for (lr, lg, lb), (rr, rg, rb) in zip(left, right):
        total += abs(lr - rr) + abs(lg - rg) + abs(lb - rb)
    return total / (len(left) * 3)


def _hamming(left: int, right: int) -> int:
    """计算两个哈希的汉明距离。

    Args:
        left: 哈希一。
        right: 哈希二。

    Returns:
        int: 不同位的个数。
    """

    return (left ^ right).bit_count()


def dedupe_frames(
    paths: Iterable[Path],
    *,
    threshold: int = 3,
    color_threshold: float = 12.0,
) -> list[Path]:
    """剔除近似重复的帧，保留时间顺序。

    判重条件：dHash 汉明距离 ≤ threshold **且** 颜色差 ≤ color_threshold。
    两个条件同时满足才算重复 —— 结构相似但配色完全不同的镜头会保留下来。

    Args:
        paths: 按时间顺序排列的帧路径。
        threshold: 汉明距离阈值。
        color_threshold: 颜色平均通道差阈值。

    Returns:
        list[Path]: 去重后的路径列表，至少一张。
    """

    ordered = [Path(path) for path in paths if Path(path).is_file()]
    if len(ordered) <= 1:
        return ordered

    kept: list[Path] = []
    last_signature: tuple[int, list[tuple[int, int, int]]] | None = None
    for path in ordered:
        current = _signature(path)
        if current is None:
            kept.append(path)
            continue
        if last_signature is not None:
            similar_structure = _hamming(last_signature[0], current[0]) <= max(0, int(threshold))
            similar_color = _color_distance(last_signature[1], current[1]) <= float(color_threshold)
            if similar_structure and similar_color:
                continue
        kept.append(path)
        last_signature = current

    return kept or ordered[:1]


def to_base64(path: Path) -> str:
    """把图片文件读成 Base64 字符串。

    Args:
        path: 图片路径。

    Returns:
        str: Base64 文本。
    """

    return base64.b64encode(Path(path).read_bytes()).decode("utf-8")


def frame_stats(paths: Iterable[Path]) -> dict[str, Any]:
    """统计帧数量与总体积，用于日志和成本估算。

    Args:
        paths: 帧路径列表。

    Returns:
        dict: ``count`` 与 ``bytes``。
    """

    items = [Path(path) for path in paths]
    total = 0
    for item in items:
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return {"count": len(items), "bytes": total}
