# 视频识眼 · MaiBot 插件

> 让麦麦真正「看见」视频：**抽 3~5 帧 → 一次视觉调用 → 一句摘要进上下文**。
> 兼容 [SnowLuma / NapCat 统一 QQ 连接器](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter)，重复视频零消耗。

---

## 1. 为什么需要这个插件

MaiBot 配好视觉模型后能看图、看表情包，但**看不到视频**。原因不在 MaiBot，而在适配器：

统一 QQ 连接器在 `codecs/inbound/message_codec.py` 里把 OneBot 的 `video` 段直接降级成一段文本：

```python
if segment_type == "video":
    converted_segments.append(self._build_video_text_segment(segment_data))
    continue
...
# _build_video_text_segment 的输出
"[视频] 文件: 1A2B3C.mp4，大小: 2048576"
```

也就是说，`url` / `file` **根本没有进入 MaiBot**，模型只能看到「这里有个叫 1A2B3C.mp4 的文件」。

所以本插件做两件事：

1. 用这段占位文本判断「这条消息里有视频」；
2. 拿着 `message_id` 回头调用适配器的 **`get_msg`**，把原始 OneBot 段取回来（`adapter.snowluma.message.get_msg` / `adapter.napcat.message.get_msg`，两组名字行为一致）。

> 这也是本插件**只兼容、不魔改**适配器的原因：用的是适配器公开的 `@API`，没有 patch 任何一行源码。

## 2. 装上之后是什么效果

群里有人发视频并 @ 麦麦：

| | 之前 | 之后 |
|---|---|---|
| 模型看到的 | `[视频] 文件: 1A2B.mp4，大小: 2048576` | `[视频] 文件: 1A2B.mp4，大小: 2048576`<br>`[视频内容] 一只橘猫从沙发跳到窗台，随后镜头转向窗外的雨（4帧/8s）` |
| 麦麦的回答 | 「我看不到视频内容」 | 「这只猫跳得挺利索，不过外面在下雨呢」 |

注入的那句话是**普通文本段**，所以不会额外触发一次 LLM 请求——它只是把「模型缺失的那点信息」补进它本来就要读的上下文里。

## 3. 实现思路与 token 账本

### 3.1 流水线（越靠前越便宜，能提前退出就退出）

```
① 认出视频标记                     0 成本
② meta 缓存键命中 → 直接返回        0 下载 / 0 token   ← 转发、重发都走这里
③ 回查 get_msg 拿 url              一次本地 RPC
④ 预算闸门（日视频数 / 日 token）    0 成本，超限即放弃
⑤ 流式下载（体积上限 + 超时）        带宽成本
⑥ content 缓存键命中 → 直接返回     0 token            ← 改了文件名但内容相同
⑦ ffmpeg 抽帧 + dHash 去重          CPU 成本
⑧ 一次视觉请求（多图一请求）         唯一的 token 支出
```

### 3.2 四个省 token 的关键设计

**① 抽得少，而不是抽得密。**
默认「平均每 6 秒一帧」，再夹到 `2~5` 帧。一个 60 秒的视频只出 5 帧，而不是按 1fps 抽 60 帧。

**② 一次请求看完全部帧。**
把 N 张图放进**同一条** user 消息里，只付一次系统提示和对话开销；而不是「每帧调一次模型」。

**③ 图片先缩小再送。**
长边 512px、JPEG `-q:v 5`。再大的图，模型也只能看出「这是一只猫」，多出来的都是纯支出。

**④ 双键缓存 + 就地压缩。**
QQ 的视频 URL 带签名、每次都不一样，拿 URL 当缓存键必然全 miss。所以：

- **meta 键** = `文件名 + 文件大小`（从占位文本就能拿到，**不用下载**）；
- **content 键** = 视频字节的 sha256（下载后再查一次，覆盖改名的同一视频）。

命中缓存时：**0 下载、0 token**，注入文本从 1.5k token 降到 0。

### 3.3 成本对照（同一段 30 秒视频）

| 方案 | 图片 token | 请求数 | 合计 |
|---|---|---|---|
| 逐帧 1fps + 每帧一次请求 | 30 × 320 ≈ 9.6k | 30 | ≈ 30k（含重复系统提示） |
| 逐帧 1fps + 一请求多图 | 30 × 320 ≈ 9.6k | 1 | ≈ 10k |
| **本插件默认** | 5 × 320 ≈ 1.6k | 1 | **≈ 1.7k** |
| **本插件 + 缓存命中** | 0 | 0 | **0** |

> 上面是「估算」，用于预算闸门，不是账单。若模型返回了 `usage`，插件会优先用真实值记账。

### 3.4 省 token 之外，更重要的是「不打扰主链」

- 钩子里默认最多等 15 秒，超时就**放弃注入**、原消息照常往下走；
- `hook_sync_budget_ms = 0` 时钩子只做缓存命中，未命中的视频交给 `watch_video` 工具按需解析；
- 默认 `trigger_mode = mention_or_private`：群聊里刷视频不会偷偷烧钱，只有私聊或被 @ 才解析；
- 每个聊天流 30 秒冷却，避免连发视频把队列堵住；
- 全局并发 1，视频解析排队跑，不和消息主链抢 CPU。

## 4. 安装

> 插件就是一个普通目录：Runner 会扫描 `MaiBot/plugins/*/_manifest.json` 来发现它。
> 目录名叫什么不影响识别（建议叫 `video-vision`），真正的身份是 manifest 里的 `id`。

### 方式 A：插件市场（上架后推荐）

MaiBot WebUI → 插件管理 → 插件市场 → 搜索「视频识眼 / Video Vision」→ 安装 → 在插件设置里确认 `vlm` 任务已配置。

### 方式 B：git clone（测试期推荐，方便更新）

```bash
cd MaiBot/plugins
git clone https://github.com/SaltinAnon/maibot-video-vision.git video-vision
```

以后更新只需要 `cd plugins/video-vision && git pull`。

### 方式 C：从本地文件夹直装（还没上架时最快）

```bash
# Windows（Git Bash / PowerShell 均可，注意目标路径换成你自己的 MaiBot）
cp -r F:/WorkSpace/maibot_plugins/maibot01 "D:/MaiBot/plugins/video-vision"
# 或者直接复制粘贴整个 maibot01 文件夹，重命名为 video-vision
```

装完重启 MaiBot（或在 WebUI 插件页点「重载」）。首次加载会：

1. 按 `config_model` 生成 `plugins/video-vision/config.toml`；
2. 自动安装声明的 Python 依赖 `imageio-ffmpeg`（自带 ffmpeg 静态二进制，省去手动装）。

### 装完后的验收（三件事，缺一不可）

| # | 检查 | 预期 |
|---|---|---|
| 1 | WebUI → 插件管理 | 出现「视频识眼」，状态是**已加载**；日志有 `视频识眼已加载：ffmpeg=...` |
| 2 | 插件设置 | 能看到 6 个配置分区（插件/视频识别/抽帧/视觉模型/成本与限额/运行时） |
| 3 | 私聊发一段视频 | 消息里出现 `[视频内容] …`；发 `/video status` 能看到计数 |

### 加载失败怎么查

| 现象 | 原因 | 修法 |
|---|---|---|
| 插件不出现 | 目录层级错了（放成了 `plugins/video-vision/maibot01/_manifest.json`） | `_manifest.json` 必须直接位于 `plugins/<任意名>/` 下 |
| 状态红色 / 未加载 | manifest 校验失败 | 看日志里 `ManifestValidator` 的具体行；对照 `python -m json.tool _manifest.json` |
| `ModuleNotFoundError: imageio_ffmpeg` | 自动装依赖失败（网络/镜像） | 在 MaiBot 根目录执行 `uv pip install imageio-ffmpeg` |
| 日志「未找到 ffmpeg」 | 依赖没装上 | 同上，或在插件设置里填 `runtime.ffmpeg_path` |
| 有视频但没注入 | 触发条件没满足 | 群聊要 @ 机器人，或是私聊；检查 `detect.trigger_mode` 不是 `off` |
| 日志 `no_video_segment` | 适配器 `get_msg` 没返回视频段 | 确认统一 QQ 连接器版本，且视频消息没被协议端清理 |

### 依赖

| 依赖 | 说明 |
|---|---|
| MaiBot ≥ 1.3.0 | 需要 manifest v2 + 命名 Hook |
| maibot-plugin-sdk ≥ 2.6.0 | 需要 `ctx.paths`（插件专属数据目录） |
| Pillow | 宿主已有（MaiBot 依赖），用于 dHash 去重 |
| httpx | 宿主已有，用于流式下载 |
| **ffmpeg** | 按顺序找：`runtime.ffmpeg_path` → `FFMPEG_BINARY` / `IMAGEIO_FFMPEG_EXE` / `FFMPEG_PATH` → `PATH` → `imageio-ffmpeg` 自带 |

> 找不到 ffmpeg 时插件**不会静默失败**：日志里会明确写「未找到 ffmpeg」，解析时返回 `no_ffmpeg` 并跳过注入。

## 5. 配置

首次加载后在 WebUI 的插件设置里可视化编辑，或直接改 `plugins/video-vision/config.toml`（改完自动热重载）。仓库里另附一份带注释的 [`config.toml.example`](config.toml.example)。

```toml
[plugin]
config_version = "1.0.0"   # SDK 要求：配置结构变更时递增

[detect]
marker_patterns = ['\[视频\]', '\[视频消息\]', '\[video\]', 'CQ:video']
trigger_mode = "mention_or_private"  # mention_or_private | always | off
cooldown_seconds = 30
scan_recent_for_tool = 12
inject_prefix = "[视频内容]"
include_meta = true
inject_on_failure = false

[frames]
max_frames = 5
min_frames = 2
seconds_per_frame = 6.0
max_edge_px = 512
jpeg_quality = 5
dedupe = true
dedupe_threshold = 3

[vision]
task_name = "vlm"       # MaiBot 的「看图」任务，需要 visual=true 的模型
model_name = ""         # 留空用任务默认；也可强制指定
max_output_tokens = 200
temperature = 0.2
caption_max_chars = 120

[budget]
max_download_mb = 20
max_duration_seconds = 120
hook_sync_budget_ms = 15000
daily_video_limit = 60
daily_token_budget = 60000
cache_ttl_hours = 24
max_concurrent = 1
debug_keep_frames = false

[security]
allow_http = false              # 只放行 https
allow_private_hosts = false     # 拦截 loopback / 私网 / link-local
local_media_roots = []          # 允许读的本地目录；留空=完全禁止
max_redirects = 3               # 每一跳重定向都会重新校验

[runtime]
ffmpeg_path = ""
download_timeout_seconds = 30
ask_max_chars = 80
```

> `[security]` 是**默认最严**的一组开关，请先读一遍再决定是否放开：
> 视频 `url` 来自协议端返回的 OneBot 段，属于不可信输入。伪造一条事件就能让插件去探测内网
> 或读本机文件，所以默认只允许 `https` + 公网地址，本地路径一律拒绝。
> 只有当你自建协议端、媒体服务跑在同机（如 `http://127.0.0.1:3000/xxx`）且信任它时，
> 才值得把 `allow_http` / `allow_private_hosts` 打开，并把缓存目录加进 `local_media_roots`。

几个值得先改的：

| 想干什么 | 改哪里 |
|---|---|
| 群聊里也全自动解析 | `detect.trigger_mode = "always"` |
| 完全不让钩子阻塞 | `budget.hook_sync_budget_ms = 0`（只走 `watch_video` 工具） |
| 再省一半 token | `frames.max_frames = 3`、`frames.max_edge_px = 384` |
| 长视频别超时 | `budget.max_duration_seconds = 300`、适当调大 `hook_sync_budget_ms` |
| 省电/省 CPU | `frames.dedupe = true`、`budget.max_concurrent = 1` |

## 6. 命令与工具

| 触发 | 作用 |
|---|---|
| 私聊 / 群聊 @ 麦麦 + 发视频 | 自动解析并注入摘要（受 `trigger_mode` 控制） |
| `/video status` | 查看 ffmpeg 路径、今日用量、缓存命中率（操作员级） |
| `/video clear` | 清空摘要缓存（操作员级） |
| 模型调用 `watch_video` | 群聊没被 @、或追问「视频里那行字写了啥」时按需解析 |

`watch_video` 参数：`stream_id`（必填，当前聊天流）、`question`（可选，具体问题）。

## 7. 与 SnowLuma 的兼容性

| 环节 | 说明 |
|---|---|
| 视频占位文本 | 依赖适配器 `_build_video_text_segment()` 的输出格式，默认正则已覆盖 `[视频]` / `[视频消息]` |
| 取回原始段 | `adapter.snowluma.message.get_msg(message_id=...)`，失败再试 `adapter.napcat.*` |
| 下不到 url 时 | 兜底走 `adapter.snowluma.action.call_data(action_name="get_file")` |
| 发送 | 不发送新消息（只改入站消息体），因此不受适配器「群消息发送确认」机制影响 |
| 需要的 manifest 能力 | `api.call`（跨插件 API）、`llm.generate`（视觉调用）、`message.get_recent`（工具回看历史） |

> SnowLuma 是独立第三方项目（非 OSI 开源协议）。本插件**只通过 OneBot v11 标准动作**与适配器交互，不分发 SnowLuma 代码。

## 8. 排错

| 现象 | 先查什么 |
|---|---|
| 完全没反应 | `detect.trigger_mode` 是否 `off`；群聊里是否 @ 了机器人；日志里有没有「未找到 ffmpeg」 |
| 注入的是「未解析成功」 | 开了 `inject_on_failure`；关掉即可只看日志 |
| 日志报 `no_video_segment` | 适配器 `get_msg` 没返回 video 段：确认适配器版本、视频是否已被协议端清理 |
| 日志报 `no_url` | 视频段里只有 file_id：检查协议端 `get_file` 是否可用 |
| 日志报 `vision_failed` | `vlm` 任务没配模型 / 模型 `visual` 不为 true / 模型名填错 |
| 解析很慢 | 视频大 → 调小 `max_download_mb`、`max_duration_seconds`；或把 `hook_sync_budget_ms` 降到 8000 |
| 摘要太笼统 | 调大 `max_frames`（4~6）、`max_edge_px`（768），或换更强的 vlm |
| 日志报 `insecure_url` | 视频地址是 `http://` 或重定向到了非 https：正常拦截；确需放行改 `security.allow_http` |
| 日志报 `blocked_host` | 地址解析到 127.0.0.1 / 192.168.x / 10.x 等非公网 IP：同机部署才开 `security.allow_private_hosts` |
| 日志报 `local_path_not_allowed` | 协议端只给了本地路径：把它所在目录加进 `security.local_media_roots` |

## 9. 本地自检（不需要 QQ、不需要 API Key）

```bash
python tests/test_offline.py
```

会用 Pillow + ffmpeg 现场合成一段 3 秒视频，跑通「get_msg → 下载 → 抽帧 → 去重 → 视觉 → 注入」，并断言第二次同视频**视觉模型零调用**。

## 10. 目录结构

```
maibot01/
├── _manifest.json          # manifest v2：ID、版本、能力、依赖、兼容区间
├── plugin.py               # 入口：Hook + Tool + Command + 生命周期
├── config.toml.example     # 带注释的配置样例（真正的 config.toml 由 Runner 生成）
├── video_vision_core/      # 唯一包名，避免与其他插件在同一个 Runner 进程里撞名
│   ├── config_model.py     # 配置模型（WebUI 表单与 config.toml 由它生成）
│   ├── detect.py           # 视频标记识别、get_msg 载荷递归查找、CQ 码解析
│   ├── source.py           # get_msg 回查、流式下载、体积/超时限制
│   ├── ffmpeg.py           # ffmpeg 定位、元信息探测、按时间点抽帧（带退让重试）
│   ├── frames.py           # 抽帧时序规划、dHash + 颜色网格去重、Base64 编码
│   ├── vision.py           # 一次多图请求、输出清洗、token 估算
│   ├── cache.py            # 双键缓存 + 每日预算台账（原子写入）
│   └── pipeline.py         # 把上面这些串成流水线，带超时与并发控制
├── i18n/                   # zh-CN / en-US
├── tests/test_offline.py   # 离线端到端自检（不需要 QQ / API Key）
├── docs/PUBLISH.md         # 上传 GitHub + 上架插件市场
├── README.md
├── CHANGELOG.md
└── LICENSE
```

## 11. 发布与上架

见 [docs/PUBLISH.md](docs/PUBLISH.md)：从 `git init` 到插件中心搜索到你的插件，包括 Tag / Release / manifest 版本三者对齐的坑。

## 12. 已知边界

- **一条消息多个视频**：只解析第一个视频标记，避免一条消息打满预算。
- **冷却期内改名重发**：只走「文件名+大小」这个便宜键，因此不会被识别成同一个视频（要确认内容得先下载，那就不省时间了）。冷却结束后会命中内容缓存。
- **依赖宿主 vlm**：`vision.task_name = "vlm"` 需要在模型管理里配了 `visual = true` 的模型，否则会明确报 `vision_failed`。
- **视频已被协议端清理**：`get_msg` 拿不到段时会报 `no_video_segment`，这是协议端的问题，插件无法补救。

## 13. 边界与承诺

- **不越权**：只用 manifest 里声明的三个能力；不发消息、不读无关历史。
- **不静默失败**：任何一步失败都会写明确原因（`no_ffmpeg` / `no_video_segment` / `vision_failed` …）。
- **不污染主链**：钩子有超时、有冷却、有并发上限，失败一律放行原消息。
- **数据落点**：缓存写在 `data/plugins/github.saltinanon.video-vision/`，临时帧写在 `temp/plugins/…` 并在解析后删除。

## License

MIT
