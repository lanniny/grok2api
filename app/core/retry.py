"""通用重试逻辑 - 封装内层403代理池重试 + 外层可配置状态码重试"""

import asyncio
from typing import Callable, Awaitable, Any, Optional, List

from app.core.config import setting
from app.core.logger import logger


async def async_request_with_retry(
    request_func: Callable[..., Awaitable[Any]],
    *,
    log_prefix: str = "[Retry]",
    max_outer_retry: int = 3,
    max_403_retries: int = 5,
    retry_codes: Optional[List[int]] = None,
    use_proxy_pool_for_403: bool = True,
    proxy_type: str = "service",
) -> Any:
    """通用的双层重试请求函数

    Args:
        request_func: 异步请求函数，签名为 async (proxy: str|None, retry_info: dict) -> result
            - proxy: 代理URL或None
            - retry_info: {"outer_retry": int, "retry_403_count": int}
            - 返回值约定:
                - 返回 {"status_code": int, ...} 表示需要重试判断
                - 返回其他值表示成功
            - 抛出异常表示错误
        log_prefix: 日志前缀
        max_outer_retry: 外层最大重试次数
        max_403_retries: 内层403最大重试次数
        retry_codes: 可重试的HTTP状态码列表，默认从配置读取
        use_proxy_pool_for_403: 403时是否使用代理池刷新
        proxy_type: 代理类型 "service" 或 "cache"

    Returns:
        request_func 的返回值

    Raises:
        request_func 抛出的异常
    """
    if retry_codes is None:
        retry_codes = setting.grok_config.get("retry_status_codes", [401, 429])

    from app.core.proxy_pool import proxy_pool

    last_result = None

    for outer_retry in range(max_outer_retry + 1):
        retry_403_count = 0

        while retry_403_count <= max_403_retries:
            # 获取代理
            if retry_403_count > 0 and use_proxy_pool_for_403 and proxy_pool._enabled:
                logger.info(f"{log_prefix} 403重试 {retry_403_count}/{max_403_retries}，刷新代理...")
                proxy = await proxy_pool.force_refresh()
            else:
                proxy = await setting.get_proxy_async(proxy_type)

            retry_info = {
                "outer_retry": outer_retry,
                "retry_403_count": retry_403_count,
            }

            try:
                result = await request_func(proxy, retry_info)
            except Exception:
                # 异常由外层处理
                if outer_retry < max_outer_retry - 1:
                    logger.warning(f"{log_prefix} 异常，外层重试 ({outer_retry+1}/{max_outer_retry})...")
                    await asyncio.sleep(0.5)
                    break  # 跳到外层下一次
                raise

            # 检查是否为需要重试的响应
            if not isinstance(result, dict) or "status_code" not in result:
                # 成功
                if outer_retry > 0 or retry_403_count > 0:
                    logger.info(f"{log_prefix} 重试成功！")
                return result

            status_code = result["status_code"]

            # 内层403重试
            if status_code == 403 and use_proxy_pool_for_403 and proxy_pool._enabled:
                retry_403_count += 1
                if retry_403_count <= max_403_retries:
                    logger.warning(f"{log_prefix} 遇到403错误，正在重试 ({retry_403_count}/{max_403_retries})...")
                    await asyncio.sleep(0.5)
                    continue
                logger.error(f"{log_prefix} 403错误，已重试{retry_403_count-1}次，放弃")
                last_result = result
                break

            # 外层可配置状态码重试
            if status_code in retry_codes:
                if outer_retry < max_outer_retry:
                    delay = (outer_retry + 1) * 0.1
                    logger.warning(f"{log_prefix} 遇到{status_code}错误，外层重试 ({outer_retry+1}/{max_outer_retry})，等待{delay}s...")
                    await asyncio.sleep(delay)
                    last_result = result
                    break  # 跳出内层，进入外层重试
                else:
                    logger.error(f"{log_prefix} {status_code}错误，已重试{outer_retry}次，放弃")
                    last_result = result
                    break

            # 非重试状态码，直接返回让调用方处理
            return result

        else:
            # 内层 while 正常结束（403重试全部失败），且未被 break
            last_result = last_result or {"status_code": 403, "exhausted": True}

    # 所有重试用尽，返回最后结果让调用方处理
    return last_result
