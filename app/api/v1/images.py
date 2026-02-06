"""图片服务API - 代理缓存的图片和视频文件，支持按需下载"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.logger import logger
from app.services.grok.cache import image_cache_service, video_cache_service


router = APIRouter()


@router.get("/images/{img_path:path}")
async def get_image(img_path: str):
    """获取缓存的图片或视频，缓存不存在时从 assets.grok.com 按需下载

    支持两种 URL 格式：
    - 新格式（保留斜杠）: /images/users/xxx/generated/xxx/image.jpg
    - 旧格式（横杠分隔）: /images/users-xxx-generated-xxx-image.jpg
    """
    try:
        # 构造原始路径（用于 assets.grok.com 下载）
        original_path = "/" + img_path

        # 判断类型
        is_video = any(original_path.lower().endswith(ext) for ext in ['.mp4', '.webm', '.mov', '.avi'])

        if is_video:
            cache_service = video_cache_service
            media_type = "video/mp4"
        else:
            cache_service = image_cache_service
            media_type = "image/jpeg"

        # 1. 检查缓存
        cache_path = cache_service.get_cached(original_path)
        if cache_path and cache_path.exists():
            logger.debug(f"[MediaAPI] 缓存命中: {cache_path}")
            return _file_response(cache_path, media_type)

        # 2. 按需从 assets.grok.com 下载（使用任意可用 token）
        token = await _get_download_token()
        if token:
            logger.info(f"[MediaAPI] 按需下载: {original_path}")
            cache_path = await cache_service.download(original_path, token)
            if cache_path and cache_path.exists():
                return _file_response(cache_path, media_type)

        # 3. 文件不存在且无法下载
        logger.warning(f"[MediaAPI] 未找到且无法下载: {original_path}")
        raise HTTPException(status_code=404, detail="File not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MediaAPI] 获取失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _file_response(path, media_type: str) -> FileResponse:
    """构建文件响应"""
    return FileResponse(
        path=str(path),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*"
        }
    )


async def _get_download_token() -> str:
    """获取用于下载的 token"""
    try:
        from app.services.grok.token import token_manager
        from app.models.grok_models import TokenType

        tokens = token_manager.get_tokens()
        # 优先用 normal token
        for token_type in [TokenType.NORMAL.value, TokenType.SUPER.value]:
            pool = tokens.get(token_type, {})
            for sso, info in pool.items():
                if info.get("status") == "active":
                    return f"sso-rw={sso};sso={sso}"
    except Exception as e:
        logger.warning(f"[MediaAPI] 获取下载token失败: {e}")
    return ""
