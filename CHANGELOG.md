# CHANGELOG

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
