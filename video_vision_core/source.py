"""视频源解析与下载（含 SSRF / 本地文件读取防护）。

核心链路（兼容 snowluma / NapCat 统一 QQ 连接器）：

    message_id ──ctx.api.call("adapter.snowluma.message.get_msg")──> 原始 OneBot 段
                                                                     │
                                            video 段里的 url / file ─┘
                                                                     │
                                            受限下载（https + 公网地址）

为什么要绕这一圈：适配器在把消息交给 MaiBot 之前就把 video 段降级成文本了，
只有 `get_msg` 这个「回头看原始消息」的动作还能拿回 url。

⚠️ 安全边界（评审要求，默认最严）：
- 只放行 `https`，`http://` 默认拒绝（`security.allow_http` 可显式放开）；
- 解析到的 IP 不能是 loopback / 私网 / link-local / 组播 / 保留地址；
- **每一跳重定向都要重新校验**，避免「公网地址 302 到 127.0.0.1」；
- 本地路径默认完全不可读，除非把目录显式加进 `security.local_media_roots`，
  且解析符号链接后仍必须落在该目录内。

之所以防这么死：`url` / `file` 来自协议端返回的 OneBot 段，属于不可信输入。
伪造一条事件就能让插件去探测内网或读本机文件，那是不该留的口子。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .detect import find_video_payload

#: 统一 QQ 连接器暴露的 get_msg API（SnowLuma 与 NapCat 两组名称，行为一致）
GET_MSG_APIS: tuple[str, ...] = (
    "adapter.snowluma.message.get_msg",
    "adapter.napcat.message.get_msg",
)

#: 兜底：直接把动作透传给协议端（拿不到 url 时用 file_id 换文件）
CALL_DATA_APIS: tuple[str, ...] = (
    "adapter.snowluma.action.call_data",
    "adapter.napcat.action.call_data",
)

_USER_AGENT = "MaiBot-VideoVision/1.0 (+https://github.com/Mai-with-u/MaiBot)"


class SourceError(Exception):
    """视频源解析或下载失败。

    Attributes:
        reason: 机器可读的原因码，供上层拼提示语与做统计。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        """初始化异常。

        Args:
            reason: 原因码，如 ``no_video_segment``。
            detail: 便于排查的细节文本。
        """

        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class VideoSource:
    """一个可下载的视频来源。"""

    data: dict[str, Any]
    origin: str
    message_id: str = ""

    @property
    def file_name(self) -> str:
        """返回原始文件名（可能为空字符串）。"""

        value = self.data.get("file") or self.data.get("name") or ""
        return value if isinstance(value, str) else ""

    @property
    def size_hint(self) -> int:
        """返回协议端给的文件大小（字节），未知时为 0。"""

        raw = self.data.get("file_size") or self.data.get("size") or 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0


# ---------------------------------------------------------------------------
# 地址策略
# ---------------------------------------------------------------------------


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断一个 IP 是否属于「不该由插件访问」的范围。

    Args:
        ip: 已解析的 IP 对象。

    Returns:
        bool: 命中私网/环回/链路本地/组播/保留/未指定时为 True。
    """

    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _assert_host_allowed(host: str, security: Any) -> None:
    """校验主机名解析出的全部地址都是公网地址。

    逐个检查而不是只看第一个：DNS 多 A 记录时，只要有一个内网地址就拒绝。

    Args:
        host: 主机名或 IP 字面量。
        security: 安全配置（读取 ``allow_private_hosts``）。

    Raises:
        SourceError: 命中非公网地址或解析失败时抛出。
    """

    if getattr(security, "allow_private_hosts", False):
        return

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked_ip(literal):
            raise SourceError("blocked_host", f"拒绝访问非公网地址 {host}")
        return

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError("blocked_host", f"域名 {host} 解析失败: {exc}") from exc

    addresses = {str(info[4][0]).split("%")[0] for info in infos if info[4]}
    if not addresses:
        raise SourceError("blocked_host", f"域名 {host} 没有解析结果")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _blocked_ip(ip):
            raise SourceError("blocked_host", f"域名 {host} 解析到非公网地址 {raw}")


async def _validate_target(url: str, security: Any) -> None:
    """校验一个待请求的 URL：协议、凭据、主机地址。

    Args:
        url: 待校验地址。
        security: 安全配置。

    Raises:
        SourceError: 协议不允许、带凭据或主机不合法时抛出。
    """

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    allowed = {"https"}
    if getattr(security, "allow_http", False):
        allowed.add("http")
    if scheme not in allowed:
        raise SourceError(
            "insecure_url",
            f"只允许 {'/'.join(sorted(allowed))} 地址，实际是 {scheme or '未知协议'}",
        )
    if parsed.username or parsed.password:
        raise SourceError("insecure_url", "地址里不允许携带用户名/密码")
    host = parsed.hostname
    if not host:
        raise SourceError("insecure_url", "地址缺少主机名")
    await _assert_host_allowed(host, security)


# ---------------------------------------------------------------------------
# 解析：从消息 / get_msg 里找视频段
# ---------------------------------------------------------------------------


def _unwrap_payload(payload: Any) -> Any:
    """剥掉 OneBot 响应外壳，取出真正的 data。

    Args:
        payload: `get_msg` / `call_data` 的返回。

    Returns:
        Any: 尽量贴近业务数据的对象。
    """

    current = payload
    for _ in range(4):
        if not isinstance(current, Mapping):
            break
        if "retcode" in current or "status" in current:
            if "data" in current:
                current = current.get("data")
                continue
        data = current.get("data")
        if isinstance(data, Mapping) and ("message" in data or "message_type" in data):
            current = data
            continue
        break
    return current


async def _call_adapter_api(ctx: Any, api_name: str, **kwargs: Any) -> Any:
    """调用适配器公开的 API，失败时返回 None 而不是抛给调用方。

    Args:
        ctx: 插件上下文。
        api_name: 完整 API 名。
        **kwargs: API 参数。

    Returns:
        Any: 调用结果；不可用或出错时为 None。
    """

    try:
        return await ctx.api.call(api_name, version="1", **kwargs)
    except Exception:  # noqa: BLE001 - 适配器可能不存在/未授权，任何异常都只降级
        return None


def _video_from_message(message: Mapping[str, Any]) -> VideoSource | None:
    """先看消息本身有没有保留原始 video 段（未来兼容路径）。

    Args:
        message: 宿主消息字典。

    Returns:
        VideoSource | None: 命中时返回来源。
    """

    raw = message.get("raw_message")
    if isinstance(raw, list):
        found = find_video_payload(raw)
        if found:
            return VideoSource(data=found, origin="segment", message_id=str(message.get("message_id") or ""))
    return None


async def resolve_source(ctx: Any, message: Mapping[str, Any], logger: Any = None) -> VideoSource:
    """解析出一条能下载的视频源。

    Args:
        ctx: 插件上下文。
        message: 宿主消息字典。
        logger: 可选日志器。

    Returns:
        VideoSource: 视频来源。

    Raises:
        SourceError: 拿不到 message_id、适配器不可用、或原始消息里没有视频段时抛出。
    """

    direct = _video_from_message(message)
    if direct is not None:
        return direct

    message_id = str(message.get("message_id") or "").strip()
    if not message_id:
        raise SourceError("no_message_id", "消息里没有 message_id，无法回查原始视频段")

    for api_name in GET_MSG_APIS:
        payload = await _call_adapter_api(ctx, api_name, message_id=message_id)
        if payload is None:
            continue
        found = find_video_payload(_unwrap_payload(payload))
        if found:
            return VideoSource(data=found, origin=api_name, message_id=message_id)
        if logger is not None:
            logger.debug("视频插件: %s 返回成功但没有 video 段", api_name)

    raise SourceError(
        "no_video_segment",
        "适配器 get_msg 没有返回视频段（可能视频已被协议端清理，或适配器版本不支持）",
    )


# ---------------------------------------------------------------------------
# 取数：下载 / 本地读取 / Base64
# ---------------------------------------------------------------------------


async def _download(url: str, *, max_bytes: int, timeout: float, security: Any) -> bytes:
    """流式下载，逐跳校验重定向目标，边下边判体积。

    Args:
        url: 视频地址。
        max_bytes: 体积上限（字节）。
        timeout: 超时（秒）。
        security: 安全配置。

    Returns:
        bytes: 视频二进制。

    Raises:
        SourceError: 超限、地址被拒或下载失败时抛出。
    """

    max_redirects = max(0, int(getattr(security, "max_redirects", 3)))
    current = url
    buffer = bytearray()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            follow_redirects=False,  # 手动跟跳，才能逐跳校验
            trust_env=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            for _hop in range(max_redirects + 1):
                await _validate_target(current, security)
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise SourceError("redirect_failed", "重定向响应缺少 Location")
                        current = urljoin(current, location)
                        continue

                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise SourceError("too_large", f"视频体积 {int(declared)} 字节超过上限 {max_bytes}")

                    async for chunk in response.aiter_bytes(64 * 1024):
                        buffer.extend(chunk)
                        if len(buffer) > max_bytes:
                            raise SourceError("too_large", f"视频下载超过上限 {max_bytes} 字节")
                    break
            else:
                raise SourceError("redirect_failed", f"重定向超过 {max_redirects} 次")
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络错误统一归一
        raise SourceError("download_failed", f"下载失败: {exc}") from exc

    if not buffer:
        raise SourceError("download_failed", "下载结果为空")
    return bytes(buffer)


def _allowed_roots(security: Any) -> list[Path]:
    """读取并规范化允许读取的本地目录白名单。

    Args:
        security: 安全配置。

    Returns:
        list[Path]: 已解析的目录列表。
    """

    roots: list[Path] = []
    for raw in getattr(security, "local_media_roots", []) or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            roots.append(Path(os.path.expandvars(os.path.expanduser(text))).resolve())
        except OSError:
            continue
    return roots


def _read_local(path: Path, *, max_bytes: int, security: Any) -> bytes:
    """在白名单目录内读取本地文件。

    白名单为空时**一律拒绝**：协议端给出的 `file` 可能是任意路径，
    默认放行等于给伪造事件开了一个任意文件读取口。

    Args:
        path: 文件路径。
        max_bytes: 体积上限。
        security: 安全配置。

    Returns:
        bytes: 文件内容。

    Raises:
        SourceError: 目录未授权、不是普通文件、过大或读取失败时抛出。
    """

    roots = _allowed_roots(security)
    if not roots:
        raise SourceError(
            "local_path_not_allowed",
            "视频只给了本地路径，但未配置 security.local_media_roots，已拒绝读取（这是安全默认值）",
        )

    try:
        resolved = path.resolve()
    except OSError as exc:
        raise SourceError("local_path_not_allowed", f"路径无法解析: {exc}") from exc

    # resolve() 会展开符号链接，因此这里同时挡住了「软链指向白名单外」的情况
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise SourceError("local_path_not_allowed", f"{resolved} 不在允许的媒体目录内")

    if not resolved.is_file():
        raise SourceError("local_path_not_allowed", f"{resolved} 不是普通文件")

    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise SourceError("download_failed", f"本地文件不可读: {exc}") from exc
    if size > max_bytes:
        raise SourceError("too_large", f"本地视频 {size} 字节超过上限 {max_bytes}")

    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise SourceError("download_failed", f"读取本地文件失败: {exc}") from exc


def _decode_base64(value: str, *, max_bytes: int) -> bytes:
    """解码 ``base64://`` 或裸 Base64。

    Args:
        value: Base64 字符串（可带 ``base64://`` 前缀或 data URL 前缀）。
        max_bytes: 体积上限。

    Returns:
        bytes: 解码结果。

    Raises:
        SourceError: 解码失败或超限时抛出。
    """

    payload = value
    if payload.lower().startswith("base64://"):
        payload = payload[len("base64://") :]
    elif payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise SourceError("download_failed", f"Base64 解码失败: {exc}") from exc
    if len(data) > max_bytes:
        raise SourceError("too_large", f"内联视频 {len(data)} 字节超过上限 {max_bytes}")
    if not data:
        raise SourceError("download_failed", "内联视频数据为空")
    return data


async def _file_from_call_data(ctx: Any, source: VideoSource) -> dict[str, Any] | None:
    """用 file_id 走动作透传换文件信息（SnowLuma 支持 get_file）。

    Args:
        ctx: 插件上下文。
        source: 当前视频来源。

    Returns:
        dict | None: 含 url/path/base64 的字典；不可用时返回 None。
    """

    file_id = source.data.get("file_id") or source.data.get("file") or ""
    if not isinstance(file_id, str) or not file_id:
        return None
    for api_name in CALL_DATA_APIS:
        payload = await _call_adapter_api(
            ctx,
            api_name,
            action_name="get_file",
            params={"file_id": file_id},
        )
        if payload is None:
            continue
        data = _unwrap_payload(payload)
        if isinstance(data, Mapping):
            return dict(data)
    return None


async def fetch_video_bytes(
    ctx: Any,
    source: VideoSource,
    *,
    max_bytes: int,
    timeout: float,
    security: Any,
    logger: Any = None,
) -> bytes:
    """把视频源变成字节，所有取数方式都受同一套地址策略约束。

    Args:
        ctx: 插件上下文。
        source: 视频来源。
        max_bytes: 体积上限（字节）。
        timeout: 下载超时（秒）。
        security: 安全配置（https-only、私网拦截、本地目录白名单）。
        logger: 可选日志器。

    Returns:
        bytes: 视频二进制。

    Raises:
        SourceError: 所有取数方式都失败或被安全策略拒绝时抛出。
    """

    data = source.data

    inline_base64 = data.get("base64")
    if isinstance(inline_base64, str) and inline_base64.strip():
        return _decode_base64(inline_base64.strip(), max_bytes=max_bytes)

    for key in ("url", "file"):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate:
            continue
        if candidate.lower().startswith("base64://"):
            return _decode_base64(candidate, max_bytes=max_bytes)
        if candidate.lower().startswith(("http://", "https://")):
            return await _download(candidate, max_bytes=max_bytes, timeout=timeout, security=security)
        # 本地路径：必须落在白名单目录内
        local_path = Path(unquote(urlparse(candidate).path or candidate))
        if local_path.is_file() or os.path.isabs(candidate):
            return _read_local(Path(candidate), max_bytes=max_bytes, security=security)

    file_detail = await _file_from_call_data(ctx, source)
    if file_detail:
        inline = file_detail.get("base64")
        if isinstance(inline, str) and inline.strip():
            return _decode_base64(inline.strip(), max_bytes=max_bytes)
        for key in ("url", "file", "path"):
            value = file_detail.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = value.strip()
            if candidate.lower().startswith("base64://"):
                return _decode_base64(candidate, max_bytes=max_bytes)
            if candidate.lower().startswith(("http://", "https://")):
                return await _download(candidate, max_bytes=max_bytes, timeout=timeout, security=security)
            return _read_local(Path(candidate), max_bytes=max_bytes, security=security)

    if logger is not None:
        logger.debug("视频插件: 视频段字段为 %s", sorted(str(key) for key in data))
    raise SourceError("no_url", "视频段里既没有可下载的 url，也没有可访问的本地文件或 Base64")
