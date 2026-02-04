import { Hono } from "hono";
import { cors } from "hono/cors";
import type { Env } from "../env";
import { requireApiAuth } from "../auth";
import { getSettings, normalizeCfCookie } from "../settings";
import { isValidModel, MODEL_CONFIG } from "../grok/models";
import { buildConversationPayload, sendConversationRequest } from "../grok/conversation";
import { parseOpenAiFromGrokNdjson, createOpenAiStreamFromGrokNdjson } from "../grok/processor";
import { addRequestLog } from "../repo/logs";
import { applyCooldown, recordTokenFailure, selectBestToken } from "../repo/tokens";
import type { ApiAuthInfo } from "../auth";

function openAiError(message: string, code: string): Record<string, unknown> {
  return { error: { message, type: "invalid_request_error", code } };
}

function getClientIp(req: Request): string {
  return (
    req.headers.get("CF-Connecting-IP") ||
    req.headers.get("X-Forwarded-For")?.split(",")[0]?.trim() ||
    "0.0.0.0"
  );
}

export const imagesRoutes = new Hono<{ Bindings: Env; Variables: { apiAuth: ApiAuthInfo } }>();

imagesRoutes.use(
  "/*",
  cors({
    origin: "*",
    allowHeaders: ["Authorization", "Content-Type"],
    allowMethods: ["GET", "POST", "OPTIONS"],
    maxAge: 86400,
  }),
);

imagesRoutes.use("/*", requireApiAuth);

// POST /v1/images/generations
imagesRoutes.post("/generations", async (c) => {
  const start = Date.now();
  const ip = getClientIp(c.req.raw);
  const keyName = c.get("apiAuth").name ?? "Unknown";
  const origin = new URL(c.req.url).origin;

  try {
    const body = (await c.req.json()) as {
      model?: string;
      prompt?: string;
      n?: number;
      size?: string;
      response_format?: "url" | "b64_json";
      stream?: boolean;
    };

    const model = String(body.model ?? "grok-imagine-1.0");
    const prompt = String(body.prompt ?? "");
    const n = Math.min(4, Math.max(1, Number(body.n ?? 1)));
    const responseFormat = body.response_format ?? "url";
    const stream = Boolean(body.stream);

    if (!prompt) return c.json(openAiError("Missing 'prompt'", "missing_prompt"), 400);
    if (!isValidModel(model))
      return c.json(openAiError(`Model '${model}' not supported`, "model_not_supported"), 400);

    const settingsBundle = await getSettings(c.env);
    const retryCodes = Array.isArray(settingsBundle.grok.retry_status_codes)
      ? settingsBundle.grok.retry_status_codes
      : [401, 429];

    const maxRetry = 3;
    let lastErr: string | null = null;

    for (let attempt = 0; attempt < maxRetry; attempt++) {
      const chosen = await selectBestToken(c.env.DB, model);
      if (!chosen) return c.json(openAiError("No available token", "NO_AVAILABLE_TOKEN"), 503);

      const jwt = chosen.token;
      const cf = normalizeCfCookie(settingsBundle.grok.cf_clearance ?? "");
      const cookie = cf ? `sso-rw=${jwt};sso=${jwt};${cf}` : `sso-rw=${jwt};sso=${jwt}`;

      try {
        // 构建图像生成 prompt
        const imagePrompt = `Generate ${n} image(s): ${prompt}`;

        const { payload, referer } = buildConversationPayload({
          requestModel: model,
          content: imagePrompt,
          imgIds: [],
          imgUris: [],
          settings: settingsBundle.grok,
        });

        // 强制开启图像生成
        (payload as any).enableImageGeneration = true;
        (payload as any).imageGenerationCount = n;

        const upstream = await sendConversationRequest({
          payload,
          cookie,
          settings: settingsBundle.grok,
          ...(referer ? { referer } : {}),
        });

        if (!upstream.ok) {
          const txt = await upstream.text().catch(() => "");
          lastErr = `Upstream ${upstream.status}: ${txt.slice(0, 200)}`;
          await recordTokenFailure(c.env.DB, jwt, upstream.status, txt.slice(0, 200));
          await applyCooldown(c.env.DB, jwt, upstream.status);
          if (retryCodes.includes(upstream.status) && attempt < maxRetry - 1) continue;
          break;
        }

        if (stream) {
          const sse = createOpenAiStreamFromGrokNdjson(upstream, {
            cookie,
            settings: settingsBundle.grok,
            global: settingsBundle.global,
            origin,
            onFinish: async ({ status, duration }) => {
              await addRequestLog(c.env.DB, {
                ip,
                model,
                duration: Number(duration.toFixed(2)),
                status,
                key_name: keyName,
                token_suffix: jwt.slice(-6),
                error: status === 200 ? "" : "stream_error",
              });
            },
          });

          return new Response(sse, {
            status: 200,
            headers: {
              "Content-Type": "text/event-stream; charset=utf-8",
              "Cache-Control": "no-cache",
              Connection: "keep-alive",
              "X-Accel-Buffering": "no",
              "Access-Control-Allow-Origin": "*",
            },
          });
        }

        // 非流式：解析响应并提取图像 URL
        const json = await parseOpenAiFromGrokNdjson(upstream, {
          cookie,
          settings: settingsBundle.grok,
          global: settingsBundle.global,
          origin,
          requestedModel: model,
        });

        // 从响应中提取图像
        const imageUrls: string[] = [];
        const content = (json as any)?.choices?.[0]?.message?.content ?? "";

        // 解析 markdown 图片格式 ![...](url)
        const imgRegex = /!\[.*?\]\((https?:\/\/[^\s)]+)\)/g;
        let match;
        while ((match = imgRegex.exec(content)) !== null) {
          if (match[1]) imageUrls.push(match[1]);
        }

        // 构建 OpenAI 兼容的图像响应
        const imageData = imageUrls.slice(0, n).map((url) => {
          if (responseFormat === "b64_json") {
            // TODO: 如需 base64，需要额外 fetch 图片并转换
            return { url };
          }
          return { url };
        });

        const duration = (Date.now() - start) / 1000;
        await addRequestLog(c.env.DB, {
          ip,
          model,
          duration: Number(duration.toFixed(2)),
          status: 200,
          key_name: keyName,
          token_suffix: jwt.slice(-6),
          error: "",
        });

        return c.json({
          created: Math.floor(Date.now() / 1000),
          data: imageData,
        });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        lastErr = msg;
        await recordTokenFailure(c.env.DB, jwt, 500, msg);
        await applyCooldown(c.env.DB, jwt, 500);
        if (attempt < maxRetry - 1) continue;
      }
    }

    const duration = (Date.now() - start) / 1000;
    await addRequestLog(c.env.DB, {
      ip,
      model,
      duration: Number(duration.toFixed(2)),
      status: 500,
      key_name: keyName,
      token_suffix: "",
      error: lastErr ?? "unknown_error",
    });

    return c.json(openAiError(lastErr ?? "Upstream error", "upstream_error"), 500);
  } catch (e) {
    const duration = (Date.now() - start) / 1000;
    await addRequestLog(c.env.DB, {
      ip,
      model: "grok-imagine-1.0",
      duration: Number(duration.toFixed(2)),
      status: 500,
      key_name: keyName,
      token_suffix: "",
      error: e instanceof Error ? e.message : String(e),
    });
    return c.json(openAiError("Internal error", "internal_error"), 500);
  }
});

imagesRoutes.options("/*", (c) => c.body(null, 204));
