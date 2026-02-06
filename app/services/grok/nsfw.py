"""NSFW 模式服务模块 - 通过 gRPC-Web 调用 Grok AuthManagement API 开启 Unhinged 模式"""

import struct
from typing import Dict, Tuple
from urllib.parse import unquote

from curl_cffi.requests import AsyncSession

from app.core.config import setting
from app.core.logger import logger
from app.core.retry import async_request_with_retry
from app.services.grok.statsig import get_dynamic_headers


NSFW_API = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
BROWSER = "chrome133a"


# --- gRPC-Web 编解码 ---

def _encode_grpc_web_payload(data: bytes) -> bytes:
    """编码 gRPC-Web 请求 payload（5字节帧头 + 数据）"""
    frame = bytearray(5 + len(data))
    frame[0] = 0x00  # flags: 数据帧
    struct.pack_into(">I", frame, 1, len(data))  # 4字节 big-endian 长度
    frame[5:] = data
    return bytes(frame)


def _parse_grpc_web_response(body: bytes) -> Tuple[list, Dict[str, str]]:
    """解析 gRPC-Web 响应，返回 (messages, trailers)"""
    messages = []
    trailers = {}
    offset = 0

    while offset < len(body):
        if offset + 5 > len(body):
            break
        flags = body[offset]
        length = struct.unpack_from(">I", body, offset + 1)[0]
        offset += 5
        if offset + length > len(body):
            break

        data = body[offset:offset + length]
        offset += length

        if flags == 0x80:
            # Trailer 帧
            text = data.decode("utf-8", errors="replace")
            for line in text.split("\r\n"):
                idx = line.find(":")
                if idx > 0:
                    key = line[:idx].strip().lower()
                    value = line[idx + 1:].strip()
                    trailers[key] = value
        else:
            messages.append(data)

    return messages, trailers


def _get_grpc_status(trailers: Dict[str, str]) -> Tuple[int, str, bool]:
    """从 trailers 获取 gRPC 状态: (code, message, ok)"""
    code = int(trailers.get("grpc-status", "0"))
    message = unquote(trailers.get("grpc-message", ""))
    return code, message, code == 0


# --- NSFW API ---

async def enable_nsfw(sso_token: str) -> Dict:
    """开启 NSFW (Unhinged) 模式

    Args:
        sso_token: SSO token（不含前缀）

    Returns:
        {"success": bool, "message": str, "error"?: str}
    """
    # protobuf: field1(varint)=1, field2(varint)=1 → enable_unhinged=true
    protobuf = bytes([0x08, 0x01, 0x10, 0x01])
    return await _call_nsfw_api(sso_token, protobuf, "开启")


async def disable_nsfw(sso_token: str) -> Dict:
    """关闭 NSFW (Unhinged) 模式"""
    protobuf = bytes([0x08, 0x00, 0x10, 0x00])
    return await _call_nsfw_api(sso_token, protobuf, "关闭")


async def _call_nsfw_api(sso_token: str, protobuf: bytes, action: str) -> Dict:
    """执行 NSFW gRPC-Web 调用"""
    payload = _encode_grpc_web_payload(protobuf)

    dynamic_headers = get_dynamic_headers("/auth_mgmt.AuthManagement/UpdateUserFeatureControls")

    headers = {
        "Content-Type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "Cookie": f"sso={sso_token}; sso-rw={sso_token}",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    }
    if dynamic_headers.get("x-statsig-id"):
        headers["x-statsig-id"] = dynamic_headers["x-statsig-id"]

    async def do_request(proxy, retry_info):
        proxies = {"http": proxy, "https": proxy} if proxy else None
        async with AsyncSession(impersonate=BROWSER) as session:
            response = await session.post(
                NSFW_API,
                headers=headers,
                content=payload,
                timeout=30,
                proxies=proxies,
            )
            if response.status_code != 200:
                return {"status_code": response.status_code}

            body = response.content
            _, trailers = _parse_grpc_web_response(body)
            code, message, ok = _get_grpc_status(trailers)

            if ok:
                return {"success": True, "message": f"NSFW 模式已{action}"}
            return {"success": False, "message": "gRPC 调用失败", "error": message or f"Code: {code}"}

    try:
        result = await async_request_with_retry(do_request, log_prefix=f"[NSFW-{action}]")

        # 重试用尽
        if isinstance(result, dict) and "status_code" in result and "success" not in result:
            return {"success": False, "message": "HTTP 请求失败", "error": f"Status: {result['status_code']}"}

        return result

    except Exception as e:
        logger.error(f"[NSFW] {action}异常: {e}")
        return {"success": False, "message": "请求异常", "error": str(e)}
