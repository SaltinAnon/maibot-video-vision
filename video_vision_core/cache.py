"""摘要缓存与成本台账。

缓存是「省 token」最关键的一环：同一个视频被转发/重发时，token 成本应该是 0。
但 QQ 的视频 URL 带签名、每次都不一样，直接拿 URL 当键必然全 miss，所以用双键：

- **meta 键**：`文件名 + 文件大小`。从占位文本或 get_msg 就能拿到，**不用下载**，
  同一段视频重发时大概率命中 —— 命中即 0 下载、0 token。
- **content 键**：视频字节的 sha256。meta 键没命中、下载完之后再查一次，
  覆盖「改了文件名但内容相同」的情况。

数据落在插件专属目录 `data/plugins/<id>/video_cache.json`，原子写入，不会写坏。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_CACHE_VERSION = 1


def meta_key(file_name: str, size: int) -> str:
    """用文件名 + 大小生成便宜的缓存键。

    Args:
        file_name: 协议端给的文件名。
        size: 文件大小（字节）。

    Returns:
        str: 键名；缺少信息时返回空字符串。
    """

    name = (file_name or "").strip()
    if not name or size <= 0:
        return ""
    return "m:" + hashlib.sha256(f"{name}|{size}".encode("utf-8")).hexdigest()[:32]


def content_key(data: bytes) -> str:
    """用视频内容的 sha256 生成精确缓存键。

    Args:
        data: 视频字节。

    Returns:
        str: 键名。
    """

    return "c:" + hashlib.sha256(data).hexdigest()[:32]


class CacheStore:
    """线程/协程安全的 JSON 缓存与台账。"""

    def __init__(
        self,
        path: Path,
        *,
        ttl_hours: int,
        daily_video_limit: int,
        daily_token_budget: int,
        logger: Any = None,
    ) -> None:
        """初始化缓存。

        Args:
            path: JSON 文件路径。
            ttl_hours: 条目有效期（小时）。
            daily_video_limit: 每日新视频上限。
            daily_token_budget: 每日 token 估算上限。
            logger: 可选日志器。
        """

        self._path = Path(path)
        self._ttl_seconds = max(1, int(ttl_hours)) * 3600
        self._daily_video_limit = max(0, int(daily_video_limit))
        self._daily_token_budget = max(0, int(daily_token_budget))
        self._logger = logger
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        """返回空数据结构。

        Returns:
            dict: 空缓存结构。
        """

        return {
            "version": _CACHE_VERSION,
            "entries": {},
            "ledger": {"date": CacheStore._today(), "videos": 0, "tokens": 0},
            "stats": {"hits": 0, "misses": 0, "errors": 0, "tokens": 0},
        }

    @staticmethod
    def _today() -> str:
        """返回本地日期字符串。

        Returns:
            str: ``YYYY-MM-DD``。
        """

        return datetime.now().strftime("%Y-%m-%d")

    # ---------------- 读写 ----------------

    async def load(self) -> None:
        """从磁盘载入，并顺手做过期清理与日期滚动。"""

        raw: dict[str, Any] = {}
        if self._path.is_file():
            try:
                raw = json.loads(await asyncio.to_thread(self._path.read_text, "utf-8"))
            except Exception as exc:  # noqa: BLE001 - 文件损坏时重建，不影响主链路
                if self._logger is not None:
                    self._logger.warning("视频插件: 缓存文件读取失败，将重建: %s", exc)
                raw = {}

        data = self._empty()
        if isinstance(raw, dict):
            entries = raw.get("entries")
            if isinstance(entries, dict):
                data["entries"] = {str(k): v for k, v in entries.items() if isinstance(v, dict)}
            ledger = raw.get("ledger")
            if isinstance(ledger, dict):
                data["ledger"].update({k: ledger.get(k, data["ledger"][k]) for k in ("date", "videos", "tokens")})
            stats = raw.get("stats")
            if isinstance(stats, dict):
                data["stats"].update({k: stats.get(k, data["stats"][k]) for k in data["stats"]})

        self._data = data
        async with self._lock:
            self._roll_ledger()
            self._prune()

    def _roll_ledger(self) -> None:
        """跨天时把当日计数清零。"""

        today = self._today()
        ledger = self._data["ledger"]
        if ledger.get("date") != today:
            ledger["date"] = today
            ledger["videos"] = 0
            ledger["tokens"] = 0

    def _prune(self) -> None:
        """删除过期条目。"""

        now = time.time()
        entries = self._data["entries"]
        expired = [
            key
            for key, value in entries.items()
            if now - float(value.get("ts") or 0) > self._ttl_seconds
        ]
        for key in expired:
            entries.pop(key, None)

    async def save(self) -> None:
        """原子写回磁盘（先写临时文件再替换）。"""

        payload = json.dumps(self._data, ensure_ascii=False, indent=2)

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(payload, "utf-8")
            temp.replace(self._path)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该中断解析
            if self._logger is not None:
                self._logger.warning("视频插件: 缓存写入失败: %s", exc)

    # ---------------- 查询 ----------------

    async def get(self, keys: Iterable[str]) -> dict[str, Any] | None:
        """按多个候选键查缓存，命中任一即返回。

        Args:
            keys: 候选键。

        Returns:
            dict | None: 命中的记录；未命中返回 None。
        """

        async with self._lock:
            self._roll_ledger()
            entries = self._data["entries"]
            now = time.time()
            for key in keys:
                if not key:
                    continue
                record = entries.get(key)
                if not isinstance(record, dict):
                    continue
                if now - float(record.get("ts") or 0) > self._ttl_seconds:
                    entries.pop(key, None)
                    continue
                self._data["stats"]["hits"] = int(self._data["stats"].get("hits") or 0) + 1
                return dict(record)
            self._data["stats"]["misses"] = int(self._data["stats"].get("misses") or 0) + 1
            return None

    async def put(self, keys: Iterable[str], record: dict[str, Any]) -> None:
        """写入一条缓存（多个键指向同一记录）。

        Args:
            keys: 候选键。
            record: 记录内容。
        """

        async with self._lock:
            entries = self._data["entries"]
            stamp = dict(record)
            stamp["ts"] = time.time()
            for key in keys:
                if key:
                    entries[key] = stamp
            self._prune()

    # ---------------- 预算 ----------------

    async def reserve_slot(self) -> tuple[bool, str]:
        """预占一个「新视频解析」名额。

        Returns:
            tuple[bool, str]: 是否允许；不允许时给出原因。
        """

        async with self._lock:
            self._roll_ledger()
            ledger = self._data["ledger"]
            if self._daily_video_limit and int(ledger.get("videos") or 0) >= self._daily_video_limit:
                return False, f"今日视频解析额度已用完（{self._daily_video_limit} 个）"
            if self._daily_token_budget and int(ledger.get("tokens") or 0) >= self._daily_token_budget:
                return False, f"今日 token 预算已用完（约 {self._daily_token_budget}）"
            ledger["videos"] = int(ledger.get("videos") or 0) + 1
            return True, ""

    async def charge(self, tokens: int) -> None:
        """记一笔 token 消耗。

        Args:
            tokens: 估算 token。
        """

        async with self._lock:
            self._roll_ledger()
            value = max(0, int(tokens))
            self._data["ledger"]["tokens"] = int(self._data["ledger"].get("tokens") or 0) + value
            self._data["stats"]["tokens"] = int(self._data["stats"].get("tokens") or 0) + value

    async def record_error(self) -> None:
        """记一次失败。"""

        async with self._lock:
            self._data["stats"]["errors"] = int(self._data["stats"].get("errors") or 0) + 1

    async def snapshot(self) -> dict[str, Any]:
        """返回统计快照，供 `/video status` 使用。

        Returns:
            dict: 缓存条目数、命中/未命中、当日用量、剩余额度。
        """

        async with self._lock:
            self._roll_ledger()
            self._prune()
            ledger = dict(self._data["ledger"])
            entries = len(self._data["entries"])
            stats = dict(self._data["stats"])
        videos_left = max(0, self._daily_video_limit - int(ledger.get("videos") or 0)) if self._daily_video_limit else -1
        tokens_left = (
            max(0, self._daily_token_budget - int(ledger.get("tokens") or 0)) if self._daily_token_budget else -1
        )
        return {
            "entries": entries,
            "date": ledger.get("date"),
            "videos_today": int(ledger.get("videos") or 0),
            "tokens_today": int(ledger.get("tokens") or 0),
            "videos_left": videos_left,
            "tokens_left": tokens_left,
            "hits": int(stats.get("hits") or 0),
            "misses": int(stats.get("misses") or 0),
            "errors": int(stats.get("errors") or 0),
            "total_tokens": int(stats.get("tokens") or 0),
        }

    async def clear(self) -> int:
        """清空缓存，返回被删条目数。

        Returns:
            int: 删除条数。
        """

        async with self._lock:
            removed = len(self._data["entries"])
            self._data["entries"] = {}
        await self.save()
        return removed
