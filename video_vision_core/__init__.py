"""插件核心逻辑：配置模型、视频源解析、抽帧、视觉理解、缓存与流水线。

本包不依赖 `src.*`（插件运行在独立 Runner 进程里，只能使用 maibot_sdk），
因此这里只做纯 Python + 第三方库能力，所有与宿主交互都通过注入的 ctx 完成。
"""

from .config_model import VideoVisionConfig

__all__ = ["VideoVisionConfig"]
