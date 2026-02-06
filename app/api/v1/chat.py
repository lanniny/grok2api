"""聊天API路由 - OpenAI兼容的聊天接口"""

import time
import uuid
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Optional, Dict, Any
from fastapi.responses import StreamingResponse

from app.core.auth import auth_manager
from app.core.config import setting
from app.core.exception import GrokApiException
from app.core.logger import logger
from app.services.grok.client import GrokClient
from app.models.openai_schema import OpenAIChatRequest
from app.services.request_stats import request_stats
from app.services.request_logger import request_logger


router = APIRouter(prefix="/chat", tags=["聊天"])

# 并发控制信号量（延迟初始化）
_chat_semaphore: Optional[asyncio.Semaphore] = None


def _get_chat_semaphore() -> asyncio.Semaphore:
    """获取聊天并发信号量"""
    global _chat_semaphore
    if _chat_semaphore is None:
        max_concurrency = setting.global_config.get("max_request_concurrency", 50)
        _chat_semaphore = asyncio.Semaphore(max_concurrency)
        logger.info(f"[Chat] 初始化并发限制: {max_concurrency}")
    return _chat_semaphore


def reset_chat_semaphore():
    """重置信号量（配置变更时调用）"""
    global _chat_semaphore
    _chat_semaphore = None


@router.post("/completions", response_model=None)
async def chat_completions(
    request: Request,
    body: OpenAIChatRequest,
    auth_info: Dict[str, Any] = Depends(auth_manager.verify)
):
    """创建聊天补全（支持流式和非流式）"""
    start_time = time.time()
    model = body.model
    ip = request.client.host
    key_name = auth_info.get("name", "Unknown")
    request_id = uuid.uuid4().hex[:8]

    status_code = 200
    error_msg = ""

    # 并发限制检查
    sem = _get_chat_semaphore()
    if sem.locked() and sem._value == 0:
        logger.warning(f"[Chat] [{request_id}] 并发超限，拒绝请求: {key_name} @ {ip}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": "服务器繁忙，请稍后重试",
                    "type": "rate_limit_error",
                    "code": "concurrency_limit_exceeded"
                }
            }
        )

    try:
        async with sem:
            logger.info(f"[Chat] [{request_id}] 收到聊天请求: {key_name} @ {ip}")

            # 调用Grok客户端
            result = await GrokClient.openai_to_grok(body.model_dump())

            # 记录成功统计
            await request_stats.record_request(model, success=True)

            # 流式响应
            if body.stream:
                async def stream_wrapper():
                    try:
                        async for chunk in result:
                            yield chunk
                    finally:
                        # 流式结束记录日志
                        duration = time.time() - start_time
                        await request_logger.add_log(ip, model, duration, 200, key_name)

                return StreamingResponse(
                    content=stream_wrapper(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no"
                    }
                )

            # 非流式响应 - 记录日志
            duration = time.time() - start_time
            await request_logger.add_log(ip, model, duration, 200, key_name)
            return result

    except GrokApiException as e:
        status_code = e.status_code or 500
        error_msg = str(e)
        await request_stats.record_request(model, success=False)
        logger.error(f"[Chat] [{request_id}] Grok API错误: {e} - 详情: {e.details}")

        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, status_code, key_name, error=error_msg)

        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {
                    "message": error_msg,
                    "type": e.error_code or "grok_api_error",
                    "code": e.error_code or "unknown"
                }
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        status_code = 500
        error_msg = str(e)
        await request_stats.record_request(model, success=False)
        logger.error(f"[Chat] [{request_id}] 处理失败: {e}")

        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, status_code, key_name, error=error_msg)

        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": "服务器内部错误",
                    "type": "internal_error",
                    "code": "internal_server_error"
                }
            }
        )
