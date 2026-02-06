"""请求上下文变量 - 跨模块传递请求级别信息"""

import contextvars

# 当前请求的 base URL（从 Host/X-Forwarded 头自动检测）
# 用于生成图片/视频的绝对 URL，确保外部客户端能正确加载
request_base_url: contextvars.ContextVar[str] = contextvars.ContextVar('request_base_url', default='')


def get_base_url() -> str:
    """获取 base_url（优先配置，其次自动检测）"""
    from app.core.config import setting
    return setting.global_config.get("base_url", "") or request_base_url.get("")
