/**
 * gRPC-Web 工具模块
 * 用于编码/解码 gRPC-Web 格式的请求和响应
 */

/**
 * 编码 gRPC-Web 请求 payload
 * @param data 原始 protobuf 数据
 * @returns 带有 gRPC 帧头的数据
 */
export function encodeGrpcWebPayload(data: Uint8Array): Uint8Array {
  const frame = new Uint8Array(5 + data.length);
  // 第一个字节是 flags (0x00 = 数据帧)
  frame[0] = 0x00;
  // 接下来 4 字节是消息长度 (big-endian)
  new DataView(frame.buffer).setUint32(1, data.length, false);
  // 其余是消息数据
  frame.set(data, 5);
  return frame;
}

/**
 * 解析 gRPC-Web 响应
 * @param body 响应体
 * @param contentType 响应的 Content-Type
 * @returns 解析后的消息和 trailers
 */
export function parseGrpcWebResponse(body: Uint8Array, contentType: string): {
  messages: Uint8Array[];
  trailers: Record<string, string>;
} {
  const messages: Uint8Array[] = [];
  const trailers: Record<string, string> = {};

  let offset = 0;
  while (offset < body.length) {
    if (offset + 5 > body.length) break;

    const flags = body[offset];
    const length = new DataView(body.buffer, body.byteOffset + offset + 1, 4).getUint32(0, false);
    offset += 5;

    if (offset + length > body.length) break;

    const data = body.slice(offset, offset + length);
    offset += length;

    if (flags === 0x80) {
      // Trailer 帧
      const text = new TextDecoder().decode(data);
      for (const line of text.split("\r\n")) {
        const idx = line.indexOf(":");
        if (idx > 0) {
          const key = line.slice(0, idx).trim().toLowerCase();
          const value = line.slice(idx + 1).trim();
          trailers[key] = value;
        }
      }
    } else {
      // 数据帧
      messages.push(data);
    }
  }

  return { messages, trailers };
}

/**
 * 从 trailers 获取 gRPC 状态
 */
export function getGrpcStatus(trailers: Record<string, string>): {
  code: number;
  message: string;
  ok: boolean;
} {
  const code = parseInt(trailers["grpc-status"] ?? "0", 10);
  const message = decodeURIComponent(trailers["grpc-message"] ?? "");
  return { code, message, ok: code === 0 };
}
