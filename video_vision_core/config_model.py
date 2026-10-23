"""插件配置模型。

Runner 会根据本模型生成 `config.toml` 与 WebUI 表单：每个继承
`PluginConfigBase` 的嵌套字段都会成为 WebUI 里一个独立的配置分区。

设计原则（和「省 token」直接相关）：
- 默认只在「私聊 / 被 @"时解析视频，群聊里刷视频不会偷偷烧钱；
- 抽帧数量、边长、JPEG 质量都可调，默认值偏向「够看就行」；
- 每日视频数 / 估算 token 双闸门，超限后自动跳过并说明原因。
"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class DetectConfig(PluginConfigBase):
    """视频消息识别：怎么认出「这条消息里有视频」。"""

    __ui_label__ = "视频识别"
    __ui_icon__ = "scan-search"
    __ui_order__ = 10

    marker_patterns: list[str] = Field(
        default=[
            r"\[视频\]",
            r"\[视频消息\]",
            r"\[video\]",
            r"CQ:video",
        ],
        description=(
            "识别视频占位文本的正则列表。统一 QQ 连接器（SnowLuma / NapCat）会把 video 段"
            "降级成一段文本，默认形如「[视频] 文件: xxx.mp4，大小: 12345」。"
        ),
    )
    trigger_mode: Literal["mention_or_private", "always", "off"] = Field(
        default="mention_or_private",
        description=(
            "mention_or_private：仅私聊或被 @ 时解析（最省 token，推荐）；"
            "always：任何视频消息都解析（最贵）；"
            "off：钩子不解析，只保留 watch_video 工具按需调用。"
        ),
    )
    cooldown_seconds: int = Field(
        default=30,
        description="同一聊天流两次「主动解析」的最小间隔（秒），缓存命中不受限制。",
    )
    scan_recent_for_tool: int = Field(
        default=12,
        description="watch_video 工具回看多少条历史消息来寻找视频。",
    )
    inject_prefix: str = Field(
        default="[视频内容]",
        description="注入到消息里的摘要前缀，方便模型区分这是插件补充的信息。",
    )
    include_meta: bool = Field(
        default=True,
        description="摘要后是否附上「N 帧 / Xs」这类成本元信息（约 8 token）。",
    )
    inject_on_failure: bool = Field(
        default=False,
        description="解析失败时是否把原因写进上下文（默认只写日志，避免模型拿失败原因瞎编）。",
    )


class FramesConfig(PluginConfigBase):
    """抽帧策略：token 消耗的主开关就在这里。"""

    __ui_label__ = "抽帧"
    __ui_icon__ = "images"
    __ui_order__ = 20

    max_frames: int = Field(default=5, description="单个视频最多送给视觉模型的关键帧数。")
    min_frames: int = Field(default=2, description="单帧最少抽几张（短视频至少看两帧）。")
    seconds_per_frame: float = Field(
        default=6.0,
        description="平均每多少秒抽一帧；最终帧数会在 min_frames / max_frames 之间取整。",
    )
    max_edge_px: int = Field(
        default=512,
        description="关键帧长边像素上限。512 已足够看清主体，再大基本只多花 token。",
    )
    jpeg_quality: int = Field(
        default=5,
        description="ffmpeg JPEG 质量（2 最好 / 31 最差）。5 在体积和清晰度之间比较平衡。",
    )
    dedupe: bool = Field(default=True, description="用 dHash 去掉几乎一样的关键帧。")
    dedupe_threshold: int = Field(
        default=3,
        description="dHash 汉明距离阈值，小于等于该值视为重复帧（0-2 更严格，4-6 更宽松）。",
    )


class VisionConfig(PluginConfigBase):
    """视觉模型调用：走宿主已有的模型任务，不额外配置 API Key。"""

    __ui_label__ = "视觉模型"
    __ui_icon__ = "eye"
    __ui_order__ = 30

    task_name: str = Field(
        default="vlm",
        description="使用哪个模型任务。MaiBot 的「看图」任务名是 vlm，需配置 visual=true 的多模态模型。",
    )
    model_name: str = Field(
        default="",
        description="留空则用任务默认模型；填具体模型名可强制指定（如 qwen-vl-max）。",
    )
    max_output_tokens: int = Field(
        default=800,
        description="摘要输出上限。思考型模型会先花 token 思考，太小会导致正文为空（上限本身不额外计费）。",
    )
    temperature: float = Field(default=0.2, description="建议低温度，减少幻觉和发散。")
    caption_max_chars: int = Field(default=120, description="摘要正文最大字符数，超出会截断。")


class BudgetConfig(PluginConfigBase):
    """成本闸门：防止一个刷屏群把额度吃掉。"""

    __ui_label__ = "成本与限额"
    __ui_icon__ = "gauge"
    __ui_order__ = 40

    max_download_mb: int = Field(default=20, description="单个视频下载体积上限（MB），超过直接放弃。")
    max_duration_seconds: int = Field(default=120, description="只分析前 N 秒，超出部分忽略。")
    hook_sync_budget_ms: int = Field(
        default=15000,
        description=(
            "钩子里最多等多久（毫秒）。超时就不注入摘要，消息照原样继续；"
            "设为 0 表示钩子只做缓存命中，未命中的视频交给 watch_video 工具按需解析。"
        ),
    )
    daily_video_limit: int = Field(default=60, description="每天最多解析多少个新视频（缓存命中不计）。")
    daily_token_budget: int = Field(default=60000, description="每天视觉调用的估算 token 上限。")
    cache_ttl_hours: int = Field(default=24, description="摘要缓存有效期（小时）。")
    max_concurrent: int = Field(default=1, description="同时进行的视频解析任务数，1 表示排队。")
    debug_keep_frames: bool = Field(default=False, description="保留抽出来的临时帧，便于排查画质问题。")


class RuntimeConfig(PluginConfigBase):
    """运行时杂项。"""

    __ui_label__ = "运行时"
    __ui_icon__ = "terminal"
    __ui_order__ = 50

    ffmpeg_path: str = Field(
        default="",
        description="自定义 ffmpeg 可执行文件路径。留空会依次尝试环境变量、PATH、imageio-ffmpeg 自带二进制。",
    )
    download_timeout_seconds: int = Field(default=30, description="单个视频下载超时（秒）。")
    ask_max_chars: int = Field(default=80, description="watch_video 回答问题时的字数上限。")


class PluginMetaConfig(PluginConfigBase):
    """`[plugin]` 配置节。

    SDK 要求插件配置里必须有 `plugin.config_version`：配置结构变更时把它一起
    加一，Runner 才能判断该走「补齐字段」还是「按新结构重建」。
    """

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.1", description="配置结构版本号，改动配置字段时同步递增。")


class SecurityConfig(PluginConfigBase):
    """取视频时的地址策略。

    默认值刻意「最严」：视频 url 来自协议端返回的 OneBot 段，属于不可信输入，
    伪造一条事件就能让插件去探测内网或读本机文件。只有明确知道自己在干什么，
    才应该放开下面的开关。
    """

    __ui_label__ = "安全"
    __ui_icon__ = "shield"
    __ui_order__ = 45

    allow_http: bool = Field(
        default=False,
        description="是否允许 http:// 明文地址。默认只放行 https；qlogo/腾讯 CDN 都是 https，一般不用开。",
    )
    allow_private_hosts: bool = Field(
        default=False,
        description=(
            "是否允许访问 loopback / 私网 / 链路本地地址。"
            "仅当协议端把媒体服务跑在同机（如 http://127.0.0.1:3000/xxx）且你信任它时才开。"
        ),
    )
    local_media_roots: list[str] = Field(
        default=[],
        description=(
            "允许读取的本地目录白名单（如协议端的缓存目录）。留空表示完全禁止读取本地路径，"
            "这是安全默认值；填了之后解析符号链接也必须落在目录内。"
        ),
    )
    max_redirects: int = Field(default=3, description="最多跟随几次重定向（每一跳都会重新校验地址）。")


class VideoVisionConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginMetaConfig = Field(default_factory=PluginMetaConfig)
    detect: DetectConfig = Field(default_factory=DetectConfig)
    frames: FramesConfig = Field(default_factory=FramesConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
