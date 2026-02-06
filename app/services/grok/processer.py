"""Grok API 响应处理器 - 处理流式和非流式响应"""

import orjson
import uuid
import time
import asyncio
from typing import AsyncGenerator, Tuple, Any

from app.core.config import setting
from app.core.context import get_base_url
from app.core.exception import GrokApiException
from app.core.logger import logger
from app.models.openai_schema import (
    OpenAIChatCompletionResponse,
    OpenAIChatCompletionChoice,
    OpenAIChatCompletionMessage,
    OpenAIChatCompletionChunkResponse,
    OpenAIChatCompletionChunkChoice,
    OpenAIChatCompletionChunkMessage
)
from app.services.grok.cache import image_cache_service, video_cache_service


class StreamTimeoutManager:
    """流式响应超时管理"""
    
    def __init__(self, chunk_timeout: int = 120, first_timeout: int = 30, total_timeout: int = 600):
        self.chunk_timeout = chunk_timeout
        self.first_timeout = first_timeout
        self.total_timeout = total_timeout
        self.start_time = asyncio.get_event_loop().time()
        self.last_chunk_time = self.start_time
        self.first_received = False
    
    def check_timeout(self) -> Tuple[bool, str]:
        """检查超时"""
        now = asyncio.get_event_loop().time()
        
        if not self.first_received and now - self.start_time > self.first_timeout:
            return True, f"首次响应超时({self.first_timeout}秒)"
        
        if self.total_timeout > 0 and now - self.start_time > self.total_timeout:
            return True, f"总超时({self.total_timeout}秒)"
        
        if self.first_received and now - self.last_chunk_time > self.chunk_timeout:
            return True, f"数据块超时({self.chunk_timeout}秒)"
        
        return False, ""
    
    def mark_received(self):
        """标记收到数据"""
        self.last_chunk_time = asyncio.get_event_loop().time()
        self.first_received = True
    
    def duration(self) -> float:
        """获取总耗时"""
        return asyncio.get_event_loop().time() - self.start_time


class GrokResponseProcessor:
    """Grok响应处理器"""

    @staticmethod
    def _image_proxy_url(img_path: str) -> str:
        """构建图片代理 URL（始终通过本地 /images/ 端点，不直接暴露 assets.grok.com）"""
        base_url = get_base_url()
        return f"{base_url}/images/{img_path}" if base_url else f"/images/{img_path}"

    @staticmethod
    async def process_normal(response, auth_token: str, model: str = None) -> OpenAIChatCompletionResponse:
        """处理非流式响应"""
        response_closed = False
        try:
            async for chunk in response.aiter_lines():
                if not chunk:
                    continue

                data = orjson.loads(chunk)

                # 错误检查
                if error := data.get("error"):
                    error_msg = error.get('message', '未知错误')
                    error_code = "CONTENT_MODERATED" if "content-moderated" in str(error_msg).lower() else "API_ERROR"
                    raise GrokApiException(
                        f"API错误: {error_msg}",
                        error_code,
                        {"code": error.get("code")}
                    )

                grok_resp = data.get("result", {}).get("response", {})
                
                # 视频响应
                if video_resp := grok_resp.get("streamingVideoGenerationResponse"):
                    if video_url := video_resp.get("videoUrl"):
                        content = await GrokResponseProcessor._build_video_content(video_url, auth_token)
                        result = GrokResponseProcessor._build_response(content, model or "grok-imagine-0.9")
                        response_closed = True
                        response.close()
                        return result

                # 模型响应
                model_response = grok_resp.get("modelResponse")
                if not model_response:
                    continue

                if error_msg := model_response.get("error"):
                    error_code = "CONTENT_MODERATED" if "content-moderated" in str(error_msg).lower() else "MODEL_ERROR"
                    raise GrokApiException(f"模型错误: {error_msg}", error_code)

                # 构建内容
                content = model_response.get("message", "")
                model_name = model_response.get("model")

                # 处理图片
                if images := model_response.get("generatedImageUrls"):
                    content = await GrokResponseProcessor._append_images(content, images, auth_token)

                result = GrokResponseProcessor._build_response(content, model_name)
                response_closed = True
                response.close()
                return result

            raise GrokApiException("无响应数据", "NO_RESPONSE")

        except GrokApiException:
            raise  # 保留 CONTENT_MODERATED 等业务异常原样传播
        except orjson.JSONDecodeError as e:
            logger.error(f"[Processor] JSON解析失败: {e}")
            raise GrokApiException(f"JSON解析失败: {e}", "JSON_ERROR") from e
        except Exception as e:
            logger.error(f"[Processor] 处理错误: {type(e).__name__}: {e}")
            raise GrokApiException(f"响应处理错误: {e}", "PROCESS_ERROR") from e
        finally:
            if not response_closed and hasattr(response, 'close'):
                try:
                    response.close()
                except Exception as e:
                    logger.warning(f"[Processor] 关闭响应失败: {e}")

    @staticmethod
    async def process_stream(response, auth_token: str, session: Any = None) -> AsyncGenerator[str, None]:
        """处理流式响应"""
        # 状态变量
        is_image = False
        is_thinking = False
        thinking_finished = False
        model = None
        filtered_tags = setting.grok_config.get("filtered_tags", "").split(",")
        video_progress_started = False
        last_video_progress = -1
        response_closed = False
        show_thinking = setting.grok_config.get("show_thinking", True)

        # 超时管理
        timeout_mgr = StreamTimeoutManager(
            chunk_timeout=setting.grok_config.get("stream_chunk_timeout", 120),
            first_timeout=setting.grok_config.get("stream_first_response_timeout", 30),
            total_timeout=setting.grok_config.get("stream_total_timeout", 600)
        )

        def make_chunk(content: str, finish: str = None):
            """生成响应块"""
            chunk_data = OpenAIChatCompletionChunkResponse(
                id=f"chatcmpl-{uuid.uuid4()}",
                created=int(time.time()),
                model=model or "grok-4-mini-thinking-tahoe",
                choices=[OpenAIChatCompletionChunkChoice(
                    index=0,
                    delta=OpenAIChatCompletionChunkMessage(
                        role="assistant",
                        content=content
                    ) if content else {},
                    finish_reason=finish
                )]
            )
            return f"data: {chunk_data.model_dump_json()}\n\n"

        try:
            async for chunk in response.aiter_lines():
                # 超时检查
                is_timeout, timeout_msg = timeout_mgr.check_timeout()
                if is_timeout:
                    logger.warning(f"[Processor] {timeout_msg}")
                    yield make_chunk("", "stop")
                    yield "data: [DONE]\n\n"
                    return

                logger.debug(f"[Processor] 收到数据块: {len(chunk)} bytes")
                if not chunk:
                    continue

                try:
                    data = orjson.loads(chunk)

                    # 错误检查
                    if error := data.get("error"):
                        error_msg = error.get('message', '未知错误')
                        # content-moderated 需要抛异常让上层重试
                        if "content-moderated" in str(error_msg).lower():
                            raise GrokApiException(f"API错误: {error_msg}", "CONTENT_MODERATED")
                        logger.error(f"[Processor] API错误: {error_msg}")
                        yield make_chunk(f"Error: {error_msg}", "stop")
                        yield "data: [DONE]\n\n"
                        return

                    grok_resp = data.get("result", {}).get("response", {})
                    logger.debug(f"[Processor] 解析响应: {len(grok_resp)} bytes")
                    if not grok_resp:
                        continue
                    
                    timeout_mgr.mark_received()

                    # 更新模型
                    if user_resp := grok_resp.get("userResponse"):
                        if m := user_resp.get("model"):
                            model = m

                    # 视频处理
                    if video_resp := grok_resp.get("streamingVideoGenerationResponse"):
                        progress = video_resp.get("progress", 0)
                        v_url = video_resp.get("videoUrl")
                        
                        # 进度更新
                        if progress > last_video_progress:
                            last_video_progress = progress
                            if show_thinking:
                                if not video_progress_started:
                                    content = f"<think>视频已生成{progress}%\n"
                                    video_progress_started = True
                                elif progress < 100:
                                    content = f"视频已生成{progress}%\n"
                                else:
                                    content = f"视频已生成{progress}%</think>\n"
                                yield make_chunk(content)
                        
                        # 视频URL
                        if v_url:
                            logger.debug("[Processor] 视频生成完成")
                            video_content = await GrokResponseProcessor._build_video_content(v_url, auth_token)
                            yield make_chunk(video_content)
                        
                        continue

                    # 图片模式
                    if grok_resp.get("imageAttachmentInfo"):
                        is_image = True

                    token = grok_resp.get("token", "")

                    # 图片处理
                    if is_image:
                        if model_resp := grok_resp.get("modelResponse"):
                            image_mode = setting.global_config.get("image_mode", "url")
                            content = ""

                            for img in model_resp.get("generatedImageUrls", []):
                                if not img:
                                    continue
                                proxy_url = GrokResponseProcessor._image_proxy_url(img)
                                try:
                                    if image_mode == "base64":
                                        # Base64模式 - 分块发送
                                        base64_str = await image_cache_service.download_base64(f"/{img}", auth_token)
                                        if base64_str:
                                            # 分块发送大数据
                                            if not base64_str.startswith("data:"):
                                                parts = base64_str.split(",", 1)
                                                if len(parts) == 2:
                                                    yield make_chunk(f"![Generated Image](data:{parts[0]},")
                                                    # 8KB分块
                                                    for i in range(0, len(parts[1]), 8192):
                                                        yield make_chunk(parts[1][i:i+8192])
                                                    yield make_chunk(")\n")
                                                else:
                                                    yield make_chunk(f"![Generated Image]({base64_str})\n")
                                            else:
                                                yield make_chunk(f"![Generated Image]({base64_str})\n")
                                        else:
                                            yield make_chunk(f"![Generated Image]({proxy_url})\n")
                                    else:
                                        # URL模式 - 预缓存（失败不影响，/images/ 端点会按需下载）
                                        await image_cache_service.download_image(f"/{img}", auth_token)
                                        content += f"![Generated Image]({proxy_url})\n\n"
                                except Exception as e:
                                    logger.warning(f"[Processor] 处理图片失败: {e}")
                                    content += f"![Generated Image]({proxy_url})\n\n"

                            # Blank line before images for proper markdown paragraph separation
                            yield make_chunk("\n\n" + content.strip() + "\n")
                            yield make_chunk("", "stop")
                            return
                        elif token:
                            yield make_chunk(token)

                    # 对话处理
                    else:
                        if isinstance(token, list):
                            continue

                        if any(tag in token for tag in filtered_tags if token):
                            continue

                        current_is_thinking = grok_resp.get("isThinking", False)
                        message_tag = grok_resp.get("messageTag")

                        if thinking_finished and current_is_thinking:
                            continue

                        # 搜索结果处理
                        if grok_resp.get("toolUsageCardId"):
                            if web_search := grok_resp.get("webSearchResults"):
                                if current_is_thinking:
                                    if show_thinking:
                                        for result in web_search.get("results", []):
                                            title = result.get("title", "")
                                            url = result.get("url", "")
                                            preview = result.get("preview", "")
                                            preview_clean = preview.replace("\n", "") if isinstance(preview, str) else ""
                                            token += f'\n- [{title}]({url} "{preview_clean}")'
                                        token += "\n"
                                    else:
                                        continue
                                else:
                                    continue
                            else:
                                continue

                        if token:
                            content = token

                            if message_tag == "header":
                                content = f"\n\n{token}\n\n"

                            # Thinking状态切换
                            should_skip = False
                            if not is_thinking and current_is_thinking:
                                if show_thinking:
                                    content = f"<think>\n{content}"
                                else:
                                    should_skip = True
                            elif is_thinking and not current_is_thinking:
                                if show_thinking:
                                    content = f"\n</think>\n{content}"
                                thinking_finished = True
                            elif current_is_thinking:
                                if not show_thinking:
                                    should_skip = True

                            if not should_skip:
                                yield make_chunk(content)
                            
                            is_thinking = current_is_thinking

                except GrokApiException:
                    raise  # 业务异常(如CONTENT_MODERATED)向上传播，供重试层处理
                except (orjson.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.warning(f"[Processor] 解析失败: {e}")
                    continue
                except Exception as e:
                    logger.warning(f"[Processor] 处理出错: {e}")
                    continue

            yield make_chunk("", "stop")
            yield "data: [DONE]\n\n"
            logger.info(f"[Processor] 流式完成，耗时: {timeout_mgr.duration():.2f}秒")

        except GrokApiException:
            raise  # 业务异常向上传播（CONTENT_MODERATED等），供客户端重试
        except Exception as e:
            logger.error(f"[Processor] 严重错误: {e}")
            yield make_chunk(f"处理错误: {e}", "error")
            yield "data: [DONE]\n\n"
        finally:
            if not response_closed and hasattr(response, 'close'):
                try:
                    response.close()
                    logger.debug("[Processor] 响应已关闭")
                except Exception as e:
                    logger.warning(f"[Processor] 关闭失败: {e}")
            
            if session:
                try:
                    await session.close()
                    logger.debug("[Processor] 会话已关闭")
                except Exception as e:
                    logger.warning(f"[Processor] 关闭会话失败: {e}")

    @staticmethod
    async def _build_video_content(video_url: str, auth_token: str) -> str:
        """构建视频内容"""
        logger.debug(f"[Processor] 检测到视频: {video_url}")
        proxy_url = GrokResponseProcessor._image_proxy_url(video_url)

        try:
            await video_cache_service.download_video(f"/{video_url}", auth_token)
        except Exception as e:
            logger.warning(f"[Processor] 预缓存视频失败: {e}")

        return f'<video src="{proxy_url}" controls="controls" width="500" height="300"></video>\n'

    @staticmethod
    async def _append_images(content: str, images: list, auth_token: str) -> str:
        """追加图片到内容"""
        image_mode = setting.global_config.get("image_mode", "url")

        for img in images:
            if not img:
                continue
            proxy_url = GrokResponseProcessor._image_proxy_url(img)
            try:
                if image_mode == "base64":
                    base64_str = await image_cache_service.download_base64(f"/{img}", auth_token)
                    if base64_str:
                        content += f"\n![Generated Image]({base64_str})"
                    else:
                        content += f"\n![Generated Image]({proxy_url})"
                else:
                    # 预缓存（失败不影响，/images/ 端点会按需下载）
                    await image_cache_service.download_image(f"/{img}", auth_token)
                    content += f"\n![Generated Image]({proxy_url})"
            except Exception as e:
                logger.warning(f"[Processor] 处理图片失败: {e}")
                content += f"\n![Generated Image]({proxy_url})"
        
        return content

    @staticmethod
    def _build_response(content: str, model: str, prompt_text: str = "") -> OpenAIChatCompletionResponse:
        """构建响应对象"""
        # 粗略估算 token 数（字符数/4）
        prompt_tokens = max(len(prompt_text) // 4, 1) if prompt_text else 0
        completion_tokens = max(len(content) // 4, 1)
        total_tokens = prompt_tokens + completion_tokens

        return OpenAIChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4()}",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[OpenAIChatCompletionChoice(
                index=0,
                message=OpenAIChatCompletionMessage(
                    role="assistant",
                    content=content
                ),
                finish_reason="stop"
            )],
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens
            }
        )
