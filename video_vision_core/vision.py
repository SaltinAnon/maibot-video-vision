"""视觉模型调用：把关键帧一次性喂给宿主的 vlm 任务，换回一句人话。

两个刻意的取舍：
1. **一次请求看多帧**，而不是「每帧调一次」——图片 token 一样，但每次请求都要
   重复付一遍系统提示词和对话开销。
2. **只输出短文本**，不输出 JSON 数组。JSON 的引号、键名、括号都要花 token，
   而调用方（聊天模型）只需要一句能读懂的描述。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 单帧图片的 token 估算值（512px 级 JPEG，按主流 VLM 的高细节档取中位数）
TOKENS_PER_FRAME_ESTIMATE = 320
#: 一次视觉请求的固定开销（系统提示 + 排版开销）
REQUEST_OVERHEAD_TOKENS = 80

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s*")
_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")


class VisionError(Exception):
    """视觉模型调用失败。"""


@dataclass
class VisionResult:
    """视觉调用结果。"""

    text: str = ""
    frames: int = 0
    tokens_estimate: int = 0
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


def build_prompt(
    *,
    frame_count: int,
    duration: float,
    max_chars: int,
    question: str = "",
    ask_max_chars: int = 80,
) -> str:
    """构造指导语。要求「短、具体、不臆测」，并约束输出格式便于程序清洗。

    Args:
        frame_count: 帧数。
        duration: 视频时长（秒）。
        max_chars: 摘要字数上限。
        question: 需要模型回答的具体问题；为空时走「概括画面」模式。
        ask_max_chars: 回答模式下的字数上限。

    Returns:
        str: 指导语文本。
    """

    duration_text = f"约 {duration:.1f} 秒" if duration > 0 else "长度未知"
    header = (
        f"下面 {frame_count} 张图片是同一段视频（{duration_text}）按时间顺序抽取的关键帧。"
    )
    if question.strip():
        return (
            f"{header}\n"
            f"请只回答这个问题：{question.strip()}\n"
            f"要求：不超过 {ask_max_chars} 字；只依据画面里能看到的内容作答；"
            "看不到就说“画面里看不出”，不要猜测。"
        )
    return (
        f"{header}\n"
        "请用中文概括这段视频在讲什么。要求：\n"
        f"1. 第一行不超过 {max_chars} 字，直接写画面内容（主体、场景、正在发生的事），"
        "不要用“这是一段视频”“这段视频展示了”这类开场白；\n"
        "2. 如果画面里有清晰可读的文字或字幕，另起一行，以“字幕：”开头，不超过 30 字；没有就省略这一行；\n"
        "3. 不要描述配乐、旁白、镜头外的内容，也不要编造看不清的细节。"
    )


def clean_text(raw: str, *, max_chars: int) -> str:
    """把模型输出整理成可直接塞进上下文的一行文本。

    Args:
        raw: 模型原始输出。
        max_chars: 正文（不含字幕行）的最大字符数。

    Returns:
        str: 清洗后的文本；无有效内容时返回空字符串。
    """

    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    caption_parts: list[str] = []
    subtitle = ""
    for line in lines:
        stripped = _BULLET_RE.sub("", line).strip()
        if not stripped:
            continue
        if stripped.startswith(("字幕：", "字幕:")):
            subtitle = stripped[3:].strip()[:30]
            continue
        caption_parts.append(stripped)

    caption = " ".join(caption_parts)
    caption = _SPACE_RE.sub(" ", caption).strip()
    if len(caption) > max_chars:
        caption = caption[:max_chars].rstrip("，,；;。 ") + "…"

    if subtitle:
        return f"{caption}（字幕：{subtitle}）" if caption else f"字幕：{subtitle}"
    return caption


def _estimate_tokens(frames: int, prompt: str, output: str) -> int:
    """粗略估算本次调用消耗的 token。

    Args:
        frames: 帧数。
        prompt: 指导语。
        output: 模型输出。

    Returns:
        int: 估算值。
    """

    # 中文按 1 字 ≈ 1 token 粗算；这个数字只用于本地预算闸门，不用于计费。
    return int(frames * TOKENS_PER_FRAME_ESTIMATE + REQUEST_OVERHEAD_TOKENS + len(prompt) + len(output))


async def describe_frames(
    ctx: Any,
    images_base64: list[str],
    *,
    task_name: str,
    model_name: str = "",
    temperature: float = 0.2,
    max_tokens: int = 200,
    duration: float = 0.0,
    max_chars: int = 120,
    question: str = "",
    ask_max_chars: int = 80,
) -> VisionResult:
    """调用宿主视觉模型，一次请求看完全部关键帧。

    Args:
        ctx: 插件上下文。
        images_base64: JPEG 的 Base64 列表。
        task_name: 模型任务名（默认 vlm）。
        model_name: 具体模型名，空串表示用任务默认。
        temperature: 温度。
        max_tokens: 输出上限。
        duration: 视频时长，用于提示词。
        max_chars: 摘要字数上限。
        question: 可选的问题，走问答模式。
        ask_max_chars: 问答模式字数上限。

    Returns:
        VisionResult: 清洗后的文本与估算 token。

    Raises:
        VisionError: 模型调用失败或返回空内容时抛出。
    """

    if not images_base64:
        raise VisionError("没有可用的关键帧")

    prompt_text = build_prompt(
        frame_count=len(images_base64),
        duration=duration,
        max_chars=max_chars,
        question=question,
        ask_max_chars=ask_max_chars,
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for encoded in images_base64:
        content.append({"type": "image", "image_format": "jpeg", "image_base64": encoded})

    messages = [{"role": "user", "content": content}]

    kwargs: dict[str, Any] = {
        "prompt": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if task_name:
        kwargs["task_name"] = task_name
    if model_name:
        kwargs["model_name"] = model_name

    try:
        result = await ctx.llm.generate(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 统一转换成可读错误
        raise VisionError(f"视觉模型调用异常: {exc}") from exc

    if not isinstance(result, dict):
        raise VisionError("视觉模型返回结构异常")
    if not result.get("success"):
        raise VisionError(str(result.get("error") or "视觉模型返回失败"))

    raw_text = str(result.get("response") or "").strip()
    text = clean_text(raw_text, max_chars=max_chars)
    if not text:
        raise VisionError("视觉模型返回了空内容")

    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    tokens_estimate = _estimate_tokens(len(images_base64), prompt_text, raw_text)
    if usage:
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total = int(prompt_tokens) + int(completion_tokens)
        if total > 0:
            tokens_estimate = total

    return VisionResult(
        text=text,
        frames=len(images_base64),
        tokens_estimate=tokens_estimate,
        model=str(result.get("model") or model_name or task_name),
        usage=usage,
    )
