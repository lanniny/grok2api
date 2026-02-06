"""图片生成API - OpenAI 兼容的图片生成接口"""

import re
import time
import uuid
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import auth_manager
from app.core.config import setting
from app.core.context import request_base_url
from app.core.exception import GrokApiException
from app.core.logger import logger
from app.services.grok.client import GrokClient
from app.services.request_stats import request_stats
from app.services.request_logger import request_logger


router = APIRouter(prefix="/images", tags=["图片生成"])


class ImageGenerationRequest(BaseModel):
    """OpenAI 兼容的图片生成请求"""
    model: str = Field("grok-imagine-0.9", description="模型名称")
    prompt: str = Field(..., description="图片描述", min_length=1)
    n: int = Field(1, ge=1, le=4, description="生成图片数量")
    size: Optional[str] = Field(None, description="图片尺寸（兼容参数，实际由Grok决定）")
    response_format: str = Field("url", description="响应格式：url 或 b64_json")
    stream: bool = Field(False, description="是否流式响应")


@router.post("/generations")
async def create_image(
    request: Request,
    body: ImageGenerationRequest,
    auth_info: Dict[str, Any] = Depends(auth_manager.verify),
):
    """创建图片（OpenAI 兼容）"""
    start_time = time.time()
    ip = request.client.host
    key_name = auth_info.get("name", "Unknown")
    model = body.model
    request_id = uuid.uuid4().hex[:8]

    try:
        logger.info(f"[ImageGen] [{request_id}] 图片生成请求: {key_name} @ {ip}, prompt={body.prompt[:50]}...")

        # 自动检测 base_url
        if not setting.global_config.get("base_url"):
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
            if host:
                request_base_url.set(f"{scheme}://{host}")

        # 构建 chat 请求，让 Grok 生成图片
        image_prompt = f"Generate {body.n} image(s): {body.prompt}"

        chat_request = {
            "model": model,
            "messages": [{"role": "user", "content": image_prompt}],
            "stream": body.stream,
        }

        result = await GrokClient.openai_to_grok(chat_request)
        await request_stats.record_request(model, success=True)

        if body.stream:
            # 流式直接透传
            from fastapi.responses import StreamingResponse

            async def stream_wrapper():
                try:
                    async for chunk in result:
                        yield chunk
                finally:
                    duration = time.time() - start_time
                    await request_logger.add_log(ip, model, duration, 200, key_name)

            return StreamingResponse(
                content=stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                },
            )

        # 非流式：从响应中提取图片 URL
        content = ""
        if hasattr(result, "choices") and result.choices:
            content = result.choices[0].message.content or ""

        # 解析 markdown 图片链接 ![...](url)
        image_urls = re.findall(r'!\[.*?\]\((https?://[^\s)]+)\)', content)

        # 也匹配本地缓存路径
        local_urls = re.findall(r'!\[.*?\]\((/images/[^\s)]+)\)', content)
        base_url = setting.global_config.get("base_url", "")
        for local in local_urls:
            image_urls.append(f"{base_url}{local}" if base_url else local)

        image_data = []
        for url in image_urls[:body.n]:
            if body.response_format == "b64_json":
                # 需要下载图片并转 base64（简化处理，直接返回 URL）
                image_data.append({"url": url})
            else:
                image_data.append({"url": url})

        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, 200, key_name)

        return {
            "created": int(time.time()),
            "data": image_data,
        }

    except GrokApiException as e:
        status_code = e.status_code or 500
        await request_stats.record_request(model, success=False)
        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, status_code, key_name, error=str(e))
        logger.error(f"[ImageGen] [{request_id}] 错误: {e}")
        raise HTTPException(
            status_code=status_code,
            detail={"error": {"message": str(e), "type": "api_error", "code": e.error_code or "unknown"}},
        )
    except HTTPException:
        raise
    except Exception as e:
        await request_stats.record_request(model, success=False)
        duration = time.time() - start_time
        await request_logger.add_log(ip, model, duration, 500, key_name, error=str(e))
        logger.error(f"[ImageGen] [{request_id}] 内部错误: {e}")
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": "服务器内部错误", "type": "internal_error", "code": "internal_server_error"}},
        )
