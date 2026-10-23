"""离线自检：不需要 QQ、不需要 API Key，用一套假宿主把整条链路跑一遍。

覆盖内容：
1. `_manifest.json` 的基本校验（必填字段、ID 格式、三段式版本、未知字段）；
2. 配置模型能生成默认值、WebUI Schema 能生成；
3. 用 ffmpeg 现场合成一段 3 秒视频，走「get_msg → 下载 → 抽帧 → 去重 → 视觉」全流程；
4. 钩子确实把摘要插到了 `[视频]` 后面；
5. 第二次同样的视频命中 meta 缓存，**视觉模型零调用**（这是省 token 的核心断言）；
6. `watch_video` 工具与 `/video status` 命令可用。

运行：
    python tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maibot_sdk import PluginContext, PluginPaths  # noqa: E402

from video_vision_core.config_model import VideoVisionConfig  # noqa: E402
from video_vision_core.ffmpeg import find_ffmpeg  # noqa: E402
from plugin import VideoVisionPlugin, create_plugin  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。

    Args:
        name: 断言名称。
        condition: 是否通过。
        detail: 失败时附加的信息。
    """

    if condition:
        print(f"  PASS  {name}")
        return
    print(f"  FAIL  {name} {detail}")
    FAILURES.append(f"{name} {detail}".strip())


def check_manifest() -> None:
    """校验 manifest 的硬性字段。"""

    print("[1] _manifest.json")
    manifest_path = ROOT / "_manifest.json"
    raw = json.loads(manifest_path.read_text("utf-8"))

    required = {
        "manifest_version",
        "id",
        "version",
        "name",
        "description",
        "author",
        "license",
        "urls",
        "host_application",
        "sdk",
        "capabilities",
        "i18n",
    }
    check("必填字段齐全", required.issubset(raw), f"缺少 {required - set(raw)}")
    check("manifest_version == 2", raw.get("manifest_version") == 2)
    check("id 合法", bool(re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", str(raw.get("id")))))
    check("version 三段式", bool(re.fullmatch(r"\d+\.\d+\.\d+", str(raw.get("version")))))
    check("author.url 是 http(s)", str(raw.get("author", {}).get("url", "")).startswith("http"))
    check("urls.repository 不带 .git", not str(raw.get("urls", {}).get("repository", "")).endswith(".git"))
    allowed_top = {
        "manifest_version",
        "id",
        "version",
        "name",
        "description",
        "author",
        "license",
        "urls",
        "host_application",
        "sdk",
        "dependencies",
        "plugin_type",
        "display",
        "capabilities",
        "i18n",
        "llm_providers",
    }
    check("没有未声明字段", set(raw) <= allowed_top, f"多余字段 {set(raw) - allowed_top}")
    check("i18n.default_locale 在 supported_locales 里", raw["i18n"]["default_locale"] in raw["i18n"]["supported_locales"])
    for file_name in ("LICENSE", "README.md", "plugin.py"):
        check(f"根目录存在 {file_name}", (ROOT / file_name).is_file())


def check_config() -> None:
    """配置模型与 WebUI Schema。"""

    print("[2] 配置模型")
    instance = VideoVisionConfig()
    check("默认帧数区间合理", 1 <= instance.frames.min_frames <= instance.frames.max_frames <= 10)
    check("默认触发模式省 token", instance.detect.trigger_mode == "mention_or_private")
    check("默认视觉任务为 vlm", instance.vision.task_name == "vlm")

    schema = VideoVisionPlugin.build_config_schema(
        plugin_id="com.saltin.video-vision",
        plugin_name="视频识眼",
        plugin_version="1.0.0",
        plugin_description="test",
        plugin_author="Saltin",
    )
    sections = schema.get("sections", {})
    check("Schema 生成了 7 个分区（含 [plugin] 与 [security]）", len(sections) == 7, str(sorted(sections)))
    check("抽帧分区存在", "frames" in sections)
    check("安全分区存在", "security" in sections)
    check("默认只放行 https", instance.security.allow_http is False)
    check("默认拦截私网地址", instance.security.allow_private_hosts is False)
    check("默认禁止读本地文件", instance.security.local_media_roots == [])


def check_components() -> None:
    """确认三个接入点都能被 SDK 正确收集。"""

    print("[3] 组件声明")
    from maibot_sdk.components import collect_components

    components = collect_components(create_plugin())
    kinds = {c.get("type") for c in components}
    check("声明了 Hook / Tool / Command", kinds == {"HOOK_HANDLER", "TOOL", "COMMAND"}, str(kinds))

    hook = next(c for c in components if c.get("type") == "HOOK_HANDLER")
    check("钩子名是 chat.receive.before_process", hook["metadata"]["hook"] == "chat.receive.before_process")
    check("钩子是 blocking", hook["metadata"]["mode"] == "blocking")
    check("钩子排在前面", hook["metadata"]["order"] == "early")

    tool = next(c for c in components if c.get("type") == "TOOL")
    check("工具名是 watch_video", tool["name"] == "watch_video")
    params = [item["name"] for item in tool["metadata"]["parameters"]]
    check("工具参数包含 stream_id / question", params == ["stream_id", "question"], str(params))

    command = next(c for c in components if c.get("type") == "COMMAND")
    check("命令是操作员级", command["metadata"].get("permission") == "operator", str(command["metadata"]))


def make_sample_video(ffmpeg: str, target: Path) -> None:
    """用 Pillow 画几帧，再用 ffmpeg 合成一段测试视频。

    Args:
        ffmpeg: ffmpeg 路径。
        target: 输出 mp4 路径。
    """

    from PIL import Image, ImageDraw

    work = target.parent / "src-frames"
    work.mkdir(parents=True, exist_ok=True)
    colors = [((196, 30, 58), "SCENE A"), ((30, 90, 196), "SCENE B"), ((40, 160, 90), "SCENE C")]
    index = 0
    for color, label in colors:
        for _ in range(4):  # 每个场景 4 帧 @4fps = 1 秒
            image = Image.new("RGB", (640, 360), color)
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 40, 600, 320), outline=(255, 255, 255), width=6)
            draw.text((70, 150), f"{label} frame {index}", fill=(255, 255, 255))
            image.save(work / f"f{index:03d}.jpg", quality=90)
            index += 1

    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-framerate",
            "4",
            "-i",
            str(work / "f%03d.jpg"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        check=True,
        capture_output=True,
    )


class MediaHandler(BaseHTTPRequestHandler):
    """极简文件服务：额外提供 /redirect-evil 用于测试「重定向到非法协议」。"""

    directory = "."

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """处理 GET。"""

        if self.path.startswith("/redirect-evil"):
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return

        name = self.path.lstrip("/").split("?")[0]
        target = Path(self.directory) / name
        if target.is_file():
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404)

    def log_message(self, *args: object) -> None:
        """静音访问日志。"""


class FakeCtx(PluginContext):
    """只实现插件真正用到的那几条能力的假上下文。"""

    def __init__(self, data_dir: Path, runtime_dir: Path, *, video_data: dict, caption: str) -> None:
        """初始化。

        Args:
            data_dir: 持久化目录。
            runtime_dir: 临时目录。
            video_data: 假 get_msg 返回的 video 段 data（可含 url / file / file_size）。
            caption: 假视觉模型返回的文本。
        """

        self.video_data = video_data
        self.caption = caption
        self.llm_calls: list[dict] = []
        self.api_calls: list[str] = []
        self.sent: list[str] = []
        super().__init__("github.saltinanon.video-vision", self._rpc, PluginPaths(data_dir, runtime_dir))

    async def _rpc(self, method: str, plugin_id: str, payload: dict, **kwargs) -> dict:
        """模拟 Host 的 cap.call。

        Args:
            method: RPC 方法名。
            plugin_id: 插件 ID。
            payload: 载荷。
            **kwargs: 超时等参数。

        Returns:
            dict: 能力结果。
        """

        capability = payload.get("capability")
        args = payload.get("args") or {}

        if capability == "llm.generate":
            self.llm_calls.append(args)
            # 故意把图片数量藏在输出里，方便断言「一次请求看完全部帧」
            frame_count = sum(1 for part in args["prompt"][0]["content"] if part.get("type") == "image")
            return {
                "success": True,
                "response": f"{self.caption} [frames={frame_count}]",
                "model": "fake-vlm",
                "usage": {"prompt_tokens": 900 + frame_count * 300, "completion_tokens": 40},
            }

        if capability == "api.call":
            api_name = str(args.get("api_name"))
            self.api_calls.append(api_name)
            return {
                "success": True,
                "result": {
                    "message_id": args.get("message_id", ""),
                    "message": [
                        {"type": "text", "data": "看我拍的"},
                        {"type": "video", "data": dict(self.video_data)},
                    ],
                },
            }

        if capability == "message.get_recent":
            return {"success": True, "messages": self.history}

        if capability == "send.text":
            self.sent.append(str(args.get("text")))
            return {"success": True}

        return {"success": False, "error": f"unmocked capability: {capability}"}

    history: list[dict] = []


def build_message(message_id: str) -> dict:
    """构造一条「群聊里 @ 了机器人 + 视频」的消息字典。

    Args:
        message_id: 消息 ID。

    Returns:
        dict: 仿 SessionMessage 字典。
    """

    return {
        "message_id": message_id,
        "platform": "qq",
        "account_id": "10001",
        "scope": "default",
        "session_id": "qq-group-9527",
        "is_at": True,
        "is_mentioned": True,
        "message_info": {
            "user_info": {"user_id": "20002", "user_nickname": "Saltin"},
            "group_info": {"group_id": "9527", "group_name": "测试群"},
        },
        "raw_message": [
            {"type": "text", "data": "你看这个"},
            {"type": "text", "data": "[视频] 文件: demo.mp4，大小: 123456"},
        ],
    }


async def run_flow(ffmpeg: str) -> None:
    """端到端跑一遍。

    Args:
        ffmpeg: ffmpeg 路径。
    """

    print("[4] 端到端流程")
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        media_dir = temp_dir / "media"
        media_dir.mkdir()
        video_path = media_dir / "demo.mp4"
        make_sample_video(ffmpeg, video_path)
        check("示例视频已生成", video_path.stat().st_size > 0, f"{video_path.stat().st_size} bytes")

        # BaseHTTPRequestHandler 不接受 directory 关键字，用子类挂上目录
        handler_class = type("ScopedMediaHandler", (MediaHandler,), {"directory": str(media_dir)})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        video_url = f"http://127.0.0.1:{port}/demo.mp4"

        try:
            ctx = FakeCtx(
                temp_dir / "data",
                temp_dir / "runtime",
                video_data={"file": "demo.mp4", "url": video_url, "file_size": 123456},
                caption="三个纯色场景依次切换，最后停在一段白色文字上",
            )
            plugin = create_plugin()
            plugin._set_context(ctx)  # noqa: SLF001 - 测试专用注入
            plugin.set_plugin_config({})
            await plugin.on_load()

            # 安全自检：默认策略下，指向 127.0.0.1 的 http 地址必须被拒绝
            strict_result = await plugin._pipeline.analyze_message(build_message("msg-strict"))  # noqa: SLF001
            check("默认拒绝 http 明文地址", strict_result.reason == "insecure_url", f"{strict_result.reason} / {strict_result.error}")
            check("被拒绝时不调用视觉模型", len(ctx.llm_calls) == 0, str(len(ctx.llm_calls)))

            # 测试环境用本机 http 服务，显式放开（生产默认是关的）
            plugin.set_plugin_config(
                {"plugin": {"config_version": "1.0.1"}, "security": {"allow_http": True, "allow_private_hosts": True}}
            )
            await plugin.on_load()

            # --- 第一次：应该真解析 ---
            message = build_message("msg-001")
            kwargs = {"message": message}
            result = await plugin.handle_incoming(**kwargs)
            check("钩子返回 continue", result.get("action") == "continue")
            injected = [seg for seg in result["modified_kwargs"]["message"]["raw_message"] if seg.get("type") == "text"]
            note = next((seg["data"] for seg in injected if "[视频内容]" in seg["data"]), "")
            check("摘要已注入", bool(note), str(injected))
            check("摘要位置在视频占位符之后", injected[-1] is not None and "[视频内容]" in injected[-1].get("data", ""))
            check("视觉模型只调用了 1 次", len(ctx.llm_calls) == 1, str(len(ctx.llm_calls)))
            check("一次请求带了多帧", len(ctx.llm_calls[0]["prompt"][0]["content"]) >= 3, "帧数不足")
            check("走了适配器 get_msg", any("get_msg" in name for name in ctx.api_calls), str(ctx.api_calls))
            check("视觉任务用的是 vlm", ctx.llm_calls[0].get("task_name") == "vlm")

            # --- 第二次：同一条视频，应该零成本命中 ---
            before = len(ctx.llm_calls)
            message2 = build_message("msg-002")
            await plugin.handle_incoming(message=message2)
            check("重复视频没有再调视觉模型", len(ctx.llm_calls) == before, f"{len(ctx.llm_calls)} vs {before}")
            note2 = next(
                (seg["data"] for seg in message2["raw_message"] if "[视频内容]" in seg.get("data", "")),
                "",
            )
            check("缓存摘要同样注入", bool(note2), str(message2["raw_message"]))
            check("摘要里标注了缓存", "缓存" in note2, note2)

            # --- 第三次：文件名/大小都变了但内容相同，应命中 content 键 ---
            # 冷却期内只允许走 meta 键（不下载），所以这里模拟「30 秒后又来了一条」
            plugin._cooldowns.clear()  # noqa: SLF001 - 测试用：跳过冷却
            before = len(ctx.llm_calls)
            message3 = build_message("msg-003")
            message3["raw_message"][-1] = {"type": "text", "data": "[视频] 文件: renamed.mp4，大小: 987654"}
            await plugin.handle_incoming(message=message3)
            check("改名后仍命中内容缓存", len(ctx.llm_calls) == before, f"{len(ctx.llm_calls)} vs {before}")
            check(
                "改名后同样注入摘要",
                any("[视频内容]" in seg.get("data", "") for seg in message3["raw_message"]),
                str(message3["raw_message"]),
            )

            # --- 第四次：群聊里没 @ 机器人，默认应该完全不解析 ---
            before = len(ctx.llm_calls)
            quiet = build_message("msg-004")
            quiet["is_at"] = False
            quiet["is_mentioned"] = False
            quiet["raw_message"][-1] = {"type": "text", "data": "[视频] 文件: other.mp4，大小: 555"}
            quiet_result = await plugin.handle_incoming(message=quiet)
            check("未 @ 的群消息放行", quiet_result.get("action") == "continue")
            check("未 @ 的群消息不调视觉模型", len(ctx.llm_calls) == before, f"{len(ctx.llm_calls)} vs {before}")
            check(
                "未 @ 的群消息不注入",
                not any("[视频内容]" in seg.get("data", "") for seg in quiet["raw_message"]),
                str(quiet["raw_message"]),
            )

            # --- 第五次：视频地址坏掉，必须放行原消息而不是抛异常 ---
            ctx.video_data = {"file": "broken.mp4", "url": f"http://127.0.0.1:{port}/missing.mp4", "file_size": 111}
            broken = build_message("msg-005")
            broken["raw_message"][-1] = {"type": "text", "data": "[视频] 文件: broken.mp4，大小: 111"}
            broken_result = await plugin.handle_incoming(message=broken)
            check("解析失败不打断消息链", broken_result.get("action") == "continue")
            check(
                "解析失败不注入内容",
                not any("[视频内容]" in seg.get("data", "") for seg in broken["raw_message"]),
                str(broken["raw_message"]),
            )

            # --- 安全：本地路径默认拒绝，白名单内放行 ---
            local_size = video_path.stat().st_size
            ctx.video_data = {"file": str(video_path), "file_size": local_size}
            local_msg = build_message("msg-local")
            local_msg["raw_message"][-1] = {"type": "text", "data": "[视频] 文件: local-only.mp4，大小: 4242"}
            denied = await plugin._pipeline.analyze_message(local_msg)  # noqa: SLF001
            check("默认拒绝读取本地路径", denied.reason == "local_path_not_allowed", f"{denied.reason} / {denied.error}")

            plugin.set_plugin_config(
                {
                    "plugin": {"config_version": "1.0.1"},
                    "security": {
                        "allow_http": True,
                        "allow_private_hosts": True,
                        "local_media_roots": [str(media_dir)],
                    }
                }
            )
            await plugin.on_load()
            allowed = await plugin._pipeline.analyze_message(local_msg)  # noqa: SLF001
            check("白名单目录内的本地视频可读", allowed.ok, f"{allowed.reason} / {allowed.error}")

            # --- 安全：重定向到 file:// 必须被拦（逐跳校验） ---
            ctx.video_data = {"url": f"http://127.0.0.1:{port}/redirect-evil", "file_size": 7777}
            redirect_msg = build_message("msg-redir")
            redirect_msg["raw_message"][-1] = {"type": "text", "data": "[视频] 文件: redirect.mp4，大小: 7777"}
            redirected = await plugin._pipeline.analyze_message(redirect_msg)  # noqa: SLF001
            check("重定向到非法协议被拒绝", redirected.reason == "insecure_url", f"{redirected.reason} / {redirected.error}")

            # --- 工具 ---
            ctx.video_data = {"file": "demo.mp4", "url": video_url, "file_size": 123456}
            ctx.history = [build_message("msg-003")]
            tool_result = await plugin.watch_video(stream_id="qq-group-9527", question="画面里有几个场景？")
            check("watch_video 成功", bool(tool_result.get("success")), str(tool_result))
            check("工具返回了内容", bool(tool_result.get("content")))

            # --- 重启后缓存仍在 ---
            ctx2 = FakeCtx(
                temp_dir / "data",
                temp_dir / "runtime",
                video_data={"file": "demo.mp4", "url": video_url, "file_size": 123456},
                caption="这段文本不应该被用到：说明缓存没生效",
            )
            plugin2 = create_plugin()
            plugin2._set_context(ctx2)  # noqa: SLF001
            plugin2.set_plugin_config(
                {"plugin": {"config_version": "1.0.1"}, "security": {"allow_http": True, "allow_private_hosts": True}}
            )
            await plugin2.on_load()
            await plugin2.handle_incoming(message=build_message("msg-006"))
            check("重启后缓存仍然生效", len(ctx2.llm_calls) == 0, f"{len(ctx2.llm_calls)} 次调用")
            await plugin2.on_unload()

            # --- 命令 ---
            ok, text, weight = await plugin.cmd_video(matched_groups={"action": "status"})
            check("/video status 可用", ok and "视频识眼状态" in text, text[:80])
            check("命令权重是整数", isinstance(weight, int))

            ok_clear, clear_text, _ = await plugin.cmd_video(matched_groups={"action": "clear"})
            check("/video clear 可用", ok_clear and "已清空" in clear_text, clear_text)

            stats = await plugin._cache.snapshot()  # noqa: SLF001
            check("台账记录了 token", stats["total_tokens"] > 0, str(stats))
            check("缓存文件已落盘", (temp_dir / "data" / "video_cache.json").is_file())

            await plugin.on_unload()
        finally:
            server.shutdown()
            server.server_close()


def main() -> int:
    """入口。

    Returns:
        int: 进程退出码。
    """

    print(f"插件目录: {ROOT}")
    check_manifest()
    check_config()
    check_components()

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[4] 跳过端到端：本机没有 ffmpeg")
        FAILURES.append("缺少 ffmpeg，无法完成端到端验证")
    else:
        print(f"  ffmpeg: {ffmpeg}")
        asyncio.run(run_flow(ffmpeg))

    print("")
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
