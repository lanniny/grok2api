/**
 * Token 自动刷新模块
 * 定期检查冷却中的 Token 是否恢复可用
 */

import type { Env } from "../env";
import { getSettings, normalizeCfCookie } from "../settings";
import { listTokens, updateTokenLimits, updateTokenStatus, type TokenRow } from "../repo/tokens";
import { checkRateLimits } from "../grok/rateLimits";
import { dbRun } from "../db";

interface RefreshResult {
  total: number;
  checked: number;
  recovered: number;
  expired: number;
}

/**
 * 刷新冷却中的 Token
 * 检查是否已恢复可用，更新状态
 */
export async function refreshCoolingTokens(env: Env): Promise<RefreshResult> {
  const now = Date.now();
  const settings = await getSettings(env);
  const cf = normalizeCfCookie(settings.grok.cf_clearance ?? "");

  const allTokens = await listTokens(env.DB);

  // 筛选出冷却中或疑似耗尽的 Token
  const tokensToCheck = allTokens.filter((t) => {
    // 已过期的跳过
    if (t.status === "expired") return false;
    // 正在冷却中
    if (t.cooldown_until && t.cooldown_until > now) return true;
    // 额度耗尽
    const isExhausted = t.token_type === "ssoSuper"
      ? t.remaining_queries === 0 || t.heavy_remaining_queries === 0
      : t.remaining_queries === 0;
    return isExhausted;
  });

  const result: RefreshResult = {
    total: tokensToCheck.length,
    checked: 0,
    recovered: 0,
    expired: 0,
  };

  for (const t of tokensToCheck) {
    const cookie = cf ? `sso-rw=${t.token};sso=${t.token};${cf}` : `sso-rw=${t.token};sso=${t.token}`;

    try {
      const rateLimitResult = await checkRateLimits(cookie, settings.grok, "grok-4-fast");
      result.checked += 1;

      if (rateLimitResult) {
        const remaining = (rateLimitResult as any).remainingTokens;
        if (typeof remaining === "number" && remaining > 0) {
          // Token 已恢复
          await updateTokenLimits(env.DB, t.token, { remaining_queries: remaining });
          // 清除冷却时间
          await dbRun(
            env.DB,
            "UPDATE tokens SET cooldown_until = NULL WHERE token = ?",
            [t.token],
          );
          result.recovered += 1;
        } else if (remaining === 0) {
          // 仍然耗尽
          await updateTokenLimits(env.DB, t.token, { remaining_queries: 0 });
        }
      } else {
        // 无法获取限额信息，可能 Token 已失效
        // 如果连续多次失败可标记为 expired
        // 这里保守处理，只增加计数
      }
    } catch (e) {
      // 请求失败，跳过
    }

    // 避免请求过快
    await new Promise((res) => setTimeout(res, 100));
  }

  return result;
}

/**
 * 检查并清理过期 Token
 * 根据上次成功时间判断是否应标记为过期
 */
export async function cleanupExpiredTokens(env: Env): Promise<number> {
  const now = Date.now();
  const expirationThreshold = 7 * 24 * 60 * 60 * 1000; // 7 天未使用视为过期

  const allTokens = await listTokens(env.DB);
  let expiredCount = 0;

  for (const t of allTokens) {
    if (t.status === "expired") continue;

    // 如果 Token 创建超过 7 天且从未使用成功，标记为过期
    const age = now - t.created_time;
    if (age > expirationThreshold && t.remaining_queries === -1) {
      await updateTokenStatus(env.DB, t.token, "expired");
      expiredCount += 1;
    }
  }

  return expiredCount;
}
