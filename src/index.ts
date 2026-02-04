import { Hono } from "hono";
import type { Env } from "./env";
import { openAiRoutes } from "./routes/openai";
import { imagesRoutes } from "./routes/images";
import { mediaRoutes } from "./routes/media";
import { adminRoutes } from "./routes/admin";
import { runKvDailyClear } from "./kv/cleanup";
import { refreshCoolingTokens, cleanupExpiredTokens } from "./kv/tokenRefresh";

const app = new Hono<{ Bindings: Env }>();

app.route("/v1", openAiRoutes);
app.route("/v1/images", imagesRoutes);
app.route("/", mediaRoutes);
app.route("/", adminRoutes);

app.get("/_worker.js", (c) => c.notFound());

app.get("/", (c) => c.redirect("/login", 302));

app.get("/login", (c) =>
  c.env.ASSETS.fetch(new Request(new URL("/login.html", c.req.url), c.req.raw)),
);

app.get("/manage", (c) =>
  c.env.ASSETS.fetch(new Request(new URL("/admin.html", c.req.url), c.req.raw)),
);

app.get("/static/*", (c) => {
  const url = new URL(c.req.url);
  if (url.pathname === "/static/_worker.js") return c.notFound();
  url.pathname = url.pathname.replace(/^\/static\//, "/");
  return c.env.ASSETS.fetch(new Request(url.toString(), c.req.raw));
});

app.get("/health", (c) =>
  c.json({ status: "healthy", service: "Grok2API", runtime: "cloudflare-workers" }),
);

app.notFound((c) => {
  return c.env.ASSETS.fetch(c.req.raw);
});

const handler: ExportedHandler<Env> = {
  fetch: (request, env, ctx) => app.fetch(request, env, ctx),
  scheduled: (event, env, ctx) => {
    // 每日清理 (UTC 16:00 = 北京时间 00:00)
    ctx.waitUntil(runKvDailyClear(env));
    // Token 刷新 (每 8 小时)
    ctx.waitUntil(refreshCoolingTokens(env));
    // 清理过期 Token
    ctx.waitUntil(cleanupExpiredTokens(env));
  },
};

export default handler;
