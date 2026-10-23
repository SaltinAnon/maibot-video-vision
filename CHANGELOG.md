# CHANGELOG

## 1.0.1 — 2026-10-23

按插件中心审核意见（Issue #944）做的安全加固与文档修正。

### 安全（SSRF / 本地文件读取）

- 新增 `[security]` 配置节，默认最严：
  - `allow_http = false`：只放行 `https`，`http://` 明文默认拒绝；
  - `allow_private_hosts = false`：解析出的 IP 出现 loopback / 私网 / link-local / 组播 / 保留地址即拒绝（多 A 记录逐个检查）；
  - `local_media_roots = []`：本地路径默认**完全禁止**读取，配置白名单后仍要求 `resolve()` 展开符号链接后落在目录内；
  - `max_redirects = 3`：改为手动跟跳，**每一跳都重新校验协议与主机**，避免「公网 302 到 127.0.0.1」。
- 拒绝带用户名/密码的 URL。
- 新增错误码：`insecure_url` / `blocked_host` / `local_path_not_allowed` / `redirect_failed`。

### 修复

- **致命**：`VideoVisionPlugin.__init__` 漏调 `super().__init__()`，导致宿主 `get_components()`
  抛 `AttributeError: '_dynamic_api_components'`，**整个第三方 Runner 退出、所有插件停摆**。
  已补上基类初始化，并在自检里增加 `get_components()` 回归断言。
- `watch_video` 不再把 `stream_id` 暴露成工具参数：宿主的工具参数组装逻辑会让模型提供的同名字段
  覆盖上下文注入值，等于允许模型指定去翻哪个聊天流。现在只认宿主注入的会话 ID，缺失即拒绝执行。
- `_manifest.json` 的 `urls.repository` / `homepage` / `documentation` / `issues` 指向错误账号（`SaltinA` → `SaltinAnon`），市场链接曾 404。
- README 中的数据目录名与真实插件 ID 对齐（`github.saltinanon.video-vision`）。

### 兼容性

- 兼容「思考型」视觉模型：`response` 为空时依次尝试 `content` / `text` / `reasoning`，避免正文被思考过程挤掉时直接判失败。
- `vision.max_output_tokens` 默认 200 → 800（`max_tokens` 只是上限，不按上限计费）。
- 失败日志改为携带模型返回结构（`_brief()`），能直接看出是空内容、截断还是鉴权错误。

### 测试

- 自检新增三条安全断言：默认拒绝 http、默认拒绝本地路径、重定向到 `file://` 被拦。

## 1.0.0 — 2026-10-23

首个版本。

### 功能

- 识别入站视频消息（兼容统一 QQ 连接器把 `video` 段降级成的 `[视频] 文件: xxx.mp4，大小: N` 文本）。
- 通过适配器公开 API `adapter.snowluma.message.get_msg` / `adapter.napcat.message.get_msg` 回查原始 OneBot 段，取回视频 `url`。
- ffmpeg 按时间点抽帧（默认 2~5 帧、长边 512px、JPEG q=5），dHash + 4×4 颜色网格去重。
- 一次视觉请求理解全部关键帧，摘要以文本段注入 `chat.receive.before_process`，聊天模型无需额外请求即可「看见」视频。
- `watch_video` 工具：模型按需解析最近视频，支持针对画面提问。
- `/video status`、`/video clear` 管理命令（操作员级）。
- 双键缓存（`文件名+大小` / 内容 sha256）与每日视频数、token 双预算闸门。
- 钩子超时、聊天流冷却、并发上限：解析失败一律放行原消息。

### 已知边界

- 一条消息里多个视频只解析第一个。
- 冷却期内只走「便宜缓存键」，改名重发的视频不会被识别为同一内容（需要下载才能确认）。
- 依赖宿主 `vlm` 任务配置了 `visual = true` 的多模态模型。
