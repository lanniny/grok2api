/**
 * NSFW 模式服务模块
 * 通过 gRPC-Web 调用 Grok 的 AuthManagement API 开启 Unhinged 模式
 */

import type { GrokSettings } from "../settings";
import { encodeGrpcWebPayload, parseGrpcWebResponse, getGrpcStatus } from "./grpcWeb";
import { getDynamicHeaders } from "./headers";

const NSFW_API = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls";

export interface NSFWResult {
  success: boolean;
  message: string;
  error?: string;
}

/**
 * 开启 NSFW (Unhinged) 模式
 * @param token SSO token
 * @param settings Grok 设置
 * @returns 操作结果
 */
export async function enableNSFW(token: string, settings: GrokSettings): Promise<NSFWResult> {
  try {
    // protobuf 编码: enable_unhinged=true
    // 字段1 (varint): 1 -> 0x08 0x01
    // 字段2 (varint): 1 -> 0x10 0x01
    const protobuf = new Uint8Array([0x08, 0x01, 0x10, 0x01]);
    const payload = encodeGrpcWebPayload(protobuf);

    const headers: Record<string, string> = {
      "content-type": "application/grpc-web+proto",
      "x-grpc-web": "1",
      cookie: `sso=${token}; sso-rw=${token}`,
      accept: "*/*",
      "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
      origin: "https://grok.com",
      referer: "https://grok.com/",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    };

    // 添加动态 headers
    const dynamicHeaders = getDynamicHeaders(settings, "/auth_mgmt.AuthManagement/UpdateUserFeatureControls");
    if (dynamicHeaders["x-statsig-id"]) {
      headers["x-statsig-id"] = dynamicHeaders["x-statsig-id"];
    }

    const response = await fetch(NSFW_API, {
      method: "POST",
      headers,
      body: payload,
    });

    if (!response.ok) {
      return {
        success: false,
        message: "HTTP 请求失败",
        error: `Status: ${response.status}`,
      };
    }

    const body = new Uint8Array(await response.arrayBuffer());
    const contentType = response.headers.get("content-type") ?? "";
    const { trailers } = parseGrpcWebResponse(body, contentType);
    const status = getGrpcStatus(trailers);

    if (status.ok) {
      return {
        success: true,
        message: "NSFW 模式已开启",
      };
    }

    return {
      success: false,
      message: "gRPC 调用失败",
      error: status.message || `Code: ${status.code}`,
    };
  } catch (e) {
    return {
      success: false,
      message: "请求异常",
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

/**
 * 关闭 NSFW (Unhinged) 模式
 * @param token SSO token
 * @param settings Grok 设置
 * @returns 操作结果
 */
export async function disableNSFW(token: string, settings: GrokSettings): Promise<NSFWResult> {
  try {
    // protobuf 编码: enable_unhinged=false
    // 字段1 (varint): 0 -> 0x08 0x00
    // 字段2 (varint): 0 -> 0x10 0x00
    const protobuf = new Uint8Array([0x08, 0x00, 0x10, 0x00]);
    const payload = encodeGrpcWebPayload(protobuf);

    const headers: Record<string, string> = {
      "content-type": "application/grpc-web+proto",
      "x-grpc-web": "1",
      cookie: `sso=${token}; sso-rw=${token}`,
      accept: "*/*",
      "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
      origin: "https://grok.com",
      referer: "https://grok.com/",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    };

    const dynamicHeaders = getDynamicHeaders(settings, "/auth_mgmt.AuthManagement/UpdateUserFeatureControls");
    if (dynamicHeaders["x-statsig-id"]) {
      headers["x-statsig-id"] = dynamicHeaders["x-statsig-id"];
    }

    const response = await fetch(NSFW_API, {
      method: "POST",
      headers,
      body: payload,
    });

    if (!response.ok) {
      return {
        success: false,
        message: "HTTP 请求失败",
        error: `Status: ${response.status}`,
      };
    }

    const body = new Uint8Array(await response.arrayBuffer());
    const contentType = response.headers.get("content-type") ?? "";
    const { trailers } = parseGrpcWebResponse(body, contentType);
    const status = getGrpcStatus(trailers);

    if (status.ok) {
      return {
        success: true,
        message: "NSFW 模式已关闭",
      };
    }

    return {
      success: false,
      message: "gRPC 调用失败",
      error: status.message || `Code: ${status.code}`,
    };
  } catch (e) {
    return {
      success: false,
      message: "请求异常",
      error: e instanceof Error ? e.message : String(e),
    };
  }
}
