"""Grok API 客户端 - 处理OpenAI到Grok的请求转换和响应处理"""

import asyncio
import orjson
from typing import Dict, List, Tuple, Any, Optional
from curl_cffi.requests import AsyncSession as curl_AsyncSession

from app.core.config import setting
from app.core.logger import logger
from app.core.retry import async_request_with_retry
from app.models.grok_models import Models
from app.services.grok.processer import GrokResponseProcessor
from app.services.grok.statsig import get_dynamic_headers
from app.services.grok.token import token_manager
from app.services.grok.upload import ImageUploadManager
from app.services.grok.create import PostCreateManager
from app.core.exception import GrokApiException


# 常量
API_ENDPOINT = "https://grok.com/rest/app-chat/conversations/new"
TIMEOUT = 120
BROWSER = "chrome133a"
MAX_RETRY = 3
MAX_UPLOADS = 20  # 提高并发上传限制以支持更高并发


class GrokClient:
    """Grok API 客户端"""

    _upload_sem = None  # 延迟初始化
    _nsfw_enabled_tokens: set = set()  # 本次运行已开启NSFW的token

    @staticmethod
    def _get_upload_semaphore():
        """获取上传信号量（动态配置）"""
        if GrokClient._upload_sem is None:
            # 从配置读取，如果不可用则使用默认值
            max_concurrency = setting.global_config.get("max_upload_concurrency", MAX_UPLOADS)
            GrokClient._upload_sem = asyncio.Semaphore(max_concurrency)
            logger.debug(f"[Client] 初始化上传并发限制: {max_concurrency}")
        return GrokClient._upload_sem

    @staticmethod
    async def openai_to_grok(request: dict):
        """转换OpenAI请求为Grok请求"""
        model = request["model"]
        content, images = GrokClient._extract_content(request["messages"])
        stream = request.get("stream", False)
        
        # 获取模型信息
        info = Models.get_model_info(model)
        grok_model, mode = Models.to_grok(model)
        is_video = info.get("is_video_model", False)
        
        # 视频模型限制
        if is_video and len(images) > 1:
            logger.warning(f"[Client] 视频模型仅支持1张图片，已截取前1张")
            images = images[:1]
        
        return await GrokClient._retry(model, content, images, grok_model, mode, is_video, stream)

    @staticmethod
    async def _retry(model: str, content: str, images: List[str], grok_model: str, mode: str, is_video: bool, stream: bool):
        """重试请求"""
        last_err = None
        force_token = None  # CONTENT_MODERATED 时强制复用同一 token

        for i in range(MAX_RETRY):
            try:
                if force_token:
                    token = force_token
                    force_token = None
                else:
                    token = await token_manager.get_token(model)

                # 主动开启NSFW（仅首次）
                if setting.grok_config.get("auto_nsfw", False) and token not in GrokClient._nsfw_enabled_tokens:
                    await GrokClient._auto_enable_nsfw(token)
                    GrokClient._nsfw_enabled_tokens.add(token)

                img_ids, img_uris = await GrokClient._upload(images, token)

                # 视频模型创建会话
                post_id = None
                if is_video and img_ids and img_uris:
                    post_id = await GrokClient._create_post(img_ids[0], img_uris[0], token)

                payload = GrokClient._build_payload(content, grok_model, mode, img_ids, img_uris, is_video, post_id)
                result = await GrokClient._request(payload, token, model, stream, post_id)

                if not stream:
                    return result

                # 流式: 包装生成器以捕获迭代中的 CONTENT_MODERATED
                return GrokClient._nsfw_retry_stream(result, token, payload, model, post_id)

            except GrokApiException as e:
                last_err = e

                # 内容审核: 自动开启 NSFW 并用同一 token 重试
                if e.error_code == "CONTENT_MODERATED":
                    logger.warning(f"[Client] 内容审核触发, 自动开启NSFW, 重试 {i+1}/{MAX_RETRY}")
                    await GrokClient._auto_enable_nsfw(token)
                    force_token = token
                    if i < MAX_RETRY - 1:
                        await asyncio.sleep(0.5)
                    continue

                # 检查是否可重试
                if e.error_code not in ["HTTP_ERROR", "NO_AVAILABLE_TOKEN"]:
                    raise

                status = e.context.get("status") if e.context else None
                retry_codes = setting.grok_config.get("retry_status_codes", [401, 429])

                if status not in retry_codes:
                    raise

                if i < MAX_RETRY - 1:
                    logger.warning(f"[Client] 失败(状态:{status}), 重试 {i+1}/{MAX_RETRY}")
                    await asyncio.sleep(0.5)

        raise last_err or GrokApiException("请求失败", "REQUEST_ERROR")

    @staticmethod
    def _extract_content(messages: List[Dict]) -> Tuple[str, List[str]]:
        """提取文本和图片，保留角色结构"""
        formatted_messages = []
        images = []

        # 角色映射
        role_map = {
            "system": "系统",
            "user": "用户",
            "assistant": "grok",
            "tool": "工具",
            "developer": "系统"
        }
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            role_prefix = role_map.get(role, role)
            
            # 提取文本内容
            text_parts = []
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        if url := item.get("image_url", {}).get("url"):
                            images.append(url)
            else:
                text_parts.append(content)
            
            # 合并该消息的文本并添加角色前缀
            msg_text = "".join(text_parts).strip()
            if msg_text:
                formatted_messages.append(f"{role_prefix}：{msg_text}")
        
        # 用换行符连接所有消息
        return "\n".join(formatted_messages), images

    @staticmethod
    async def _upload(urls: List[str], token: str) -> Tuple[List[str], List[str]]:
        """并发上传图片"""
        if not urls:
            return [], []
        
        async def upload_limited(url):
            async with GrokClient._get_upload_semaphore():
                return await ImageUploadManager.upload(url, token)
        
        results = await asyncio.gather(*[upload_limited(u) for u in urls], return_exceptions=True)
        
        ids, uris = [], []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                logger.warning(f"[Client] 上传失败: {url} - {result}")
            elif isinstance(result, tuple) and len(result) == 2:
                fid, furi = result
                if fid:
                    ids.append(fid)
                    uris.append(furi)
        
        return ids, uris

    @staticmethod
    async def _create_post(file_id: str, file_uri: str, token: str) -> Optional[str]:
        """创建视频会话"""
        try:
            result = await PostCreateManager.create(file_id, file_uri, token)
            if result and result.get("success"):
                return result.get("post_id")
        except Exception as e:
            logger.warning(f"[Client] 创建会话失败: {e}")
        return None

    @staticmethod
    def _build_payload(content: str, model: str, mode: str, img_ids: List[str], img_uris: List[str], is_video: bool = False, post_id: str = None) -> Dict:
        """构建请求载荷"""
        # 视频模型特殊处理
        if is_video and img_uris:
            img_msg = f"https://grok.com/imagine/{post_id}" if post_id else f"https://assets.grok.com/post/{img_uris[0]}"
            return {
                "temporary": True,
                "modelName": "grok-3",
                "message": f"{img_msg}  {content} --mode=custom",
                "fileAttachments": img_ids,
                "toolOverrides": {"videoGen": True}
            }
        
        # 标准载荷
        return {
            "temporary": setting.grok_config.get("temporary", True),
            "modelName": model,
            "message": content,
            "fileAttachments": img_ids,
            "imageAttachments": [],
            "disableSearch": False,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "forceConcise": False,
            "toolOverrides": {},
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "isReasoning": False,
            "webpageUrls": [],
            "disableTextFollowUps": True,
            "responseMetadata": {"requestModelDetails": {"modelId": model}},
            "disableMemory": False,
            "forceSideBySide": False,
            "modelMode": mode,
            "isAsyncChat": False
        }

    @staticmethod
    async def _request(payload: dict, token: str, model: str, stream: bool, post_id: str = None):
        """发送请求"""
        if not token:
            raise GrokApiException("认证令牌缺失", "NO_AUTH_TOKEN")

        # 保存session引用，流式时不关闭
        _session_holder = {}

        async def do_request(proxy, retry_info):
            proxies = {"http": proxy, "https": proxy} if proxy else None

            headers = GrokClient._build_headers(token)
            if model == "grok-imagine-0.9":
                file_attachments = payload.get("fileAttachments", [])
                ref_id = post_id or (file_attachments[0] if file_attachments else "")
                if ref_id:
                    headers["Referer"] = f"https://grok.com/imagine/{ref_id}"

            session = curl_AsyncSession(impersonate=BROWSER)
            try:
                response = await session.post(
                    API_ENDPOINT,
                    headers=headers,
                    data=orjson.dumps(payload),
                    timeout=TIMEOUT,
                    stream=True,
                    proxies=proxies
                )

                if response.status_code != 200:
                    await session.close()
                    return {"status_code": response.status_code, "response": response}

                # 成功
                asyncio.create_task(token_manager.reset_failure(token))

                if stream:
                    _session_holder["session"] = session
                    _session_holder["response"] = response
                else:
                    try:
                        result = await GrokResponseProcessor.process_normal(response, token, model)
                    finally:
                        await session.close()
                    _session_holder["result"] = result

                return "SUCCESS"

            except GrokApiException as e:
                await session.close()
                if e.error_code == "CONTENT_MODERATED":
                    # 内容审核不需要重试，直接返回特殊标记让上层处理
                    return {"content_moderated": True, "exception": e}
                raise
            except Exception as e:
                await session.close()
                if "RequestsError" in str(type(e)):
                    raise GrokApiException(f"网络错误: {e}", "NETWORK_ERROR") from e
                raise

        result = await async_request_with_retry(do_request, log_prefix="[Client]")

        # 内容审核快速通道：绕过 async_request_with_retry 的重试直接传播
        if isinstance(result, dict) and result.get("content_moderated"):
            raise result["exception"]

        # 处理重试用尽的情况
        if isinstance(result, dict) and "status_code" in result:
            response = result.get("response")
            if response:
                GrokClient._handle_error(response, token)
            raise GrokApiException("请求失败：已达到最大重试次数", "MAX_RETRIES_EXCEEDED")

        # 成功路径
        asyncio.create_task(GrokClient._update_limits(token, model))

        if stream:
            return GrokResponseProcessor.process_stream(
                _session_holder["response"], token, _session_holder["session"]
            )
        return _session_holder["result"]


    @staticmethod
    async def _nsfw_retry_stream(stream_gen, token: str, payload: dict, model: str, post_id: str = None):
        """包装流式生成器，在迭代中捕获 CONTENT_MODERATED 并自动重试"""
        try:
            async for chunk in stream_gen:
                yield chunk
        except GrokApiException as e:
            if e.error_code != "CONTENT_MODERATED":
                raise
            logger.warning("[Client] 流式响应内容审核, 自动开启NSFW并重试")
            await GrokClient._auto_enable_nsfw(token)
            await asyncio.sleep(0.5)
            new_stream = await GrokClient._request(payload, token, model, True, post_id)
            async for chunk in new_stream:
                yield chunk

    @staticmethod
    async def _auto_enable_nsfw(auth_token: str):
        """自动开启 NSFW 模式"""
        try:
            from app.services.grok.nsfw import enable_nsfw
            sso = token_manager._extract_sso(auth_token)
            if sso:
                result = await enable_nsfw(sso)
                if result.get("success"):
                    logger.info(f"[Client] 自动开启NSFW成功: {sso[:10]}...")
                else:
                    logger.warning(f"[Client] 自动开启NSFW失败: {result.get('error', '未知')}")
            else:
                logger.warning("[Client] 无法提取SSO值, 跳过NSFW开启")
        except Exception as e:
            logger.warning(f"[Client] 自动开启NSFW异常: {e}")

    @staticmethod
    def _build_headers(token: str) -> Dict[str, str]:
        """构建请求头"""
        headers = get_dynamic_headers("/rest/app-chat/conversations/new")
        cf = setting.grok_config.get("cf_clearance", "")
        headers["Cookie"] = f"{token};{cf}" if cf else token
        return headers

    @staticmethod
    def _handle_error(response, token: str):
        """处理错误"""
        if response.status_code == 403:
            msg = "您的IP被拦截，请尝试以下方法之一: 1.更换IP 2.使用代理 3.配置CF值"
            data = {"cf_blocked": True, "status": 403}
            logger.warning(f"[Client] {msg}")
        else:
            try:
                data = response.json()
                msg = str(data)
            except:
                data = response.text
                msg = data[:200] if data else "未知错误"

        # 检测内容审核（可能来自非200响应体）
        error_code = "HTTP_ERROR"
        if "content-moderated" in str(msg).lower():
            error_code = "CONTENT_MODERATED"

        asyncio.create_task(token_manager.record_failure(token, response.status_code, msg))
        asyncio.create_task(token_manager.apply_cooldown(token, response.status_code))
        raise GrokApiException(
            f"请求失败: {response.status_code} - {msg}",
            error_code,
            {"status": response.status_code, "data": data}
        )

    @staticmethod
    async def _update_limits(token: str, model: str):
        """更新速率限制"""
        try:
            await token_manager.check_limits(token, model)
        except Exception as e:
            logger.error(f"[Client] 更新限制失败: {e}")