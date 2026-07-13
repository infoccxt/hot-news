// 前端静态资源由 ASSETS 托管；/data.json 从 KV 读取。
// KV 为空或过期时，Worker 自行从 GitHub 拉取最新 data.json 并回填 KV，
// 再定时（Cron）主动刷新——于是「采集（GitHub Actions 云端）」与「部署（Cloudflare）」
// 完全解耦，云端 Actions 不再需要任何 Cloudflare 凭证。
const UPSTREAM = "https://raw.githubusercontent.com/infoccxt/hot-news/main/data.json";
const MAX_AGE_MS = 35 * 60 * 1000; // 超过 35 分钟视为过期，触发回填

async function fetchAndCache(env) {
  const headers = {};
  if (env.GH_TOKEN) headers["Authorization"] = "Bearer " + env.GH_TOKEN;
  const r = await fetch(UPSTREAM, { headers });
  if (!r.ok) throw new Error("upstream " + r.status);
  const text = await r.text();
  JSON.parse(text); // 校验为合法 JSON 再写入，避免脏数据污染 KV
  await env.HOTNEWS_DATA.put("data.json", text);
  return text;
}

function isFresh(text) {
  try {
    const d = JSON.parse(text);
    const t = Date.parse(d.updated_at);
    if (!t) return false;
    return (Date.now() - t) < MAX_AGE_MS;
  } catch {
    return false;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/data.json") {
      try {
        const kv = await env.HOTNEWS_DATA.get("data.json");
        if (kv && isFresh(kv)) {
          return new Response(kv, {
            headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache" },
          });
        }
      } catch (e) { /* 读 KV 失败，走回填 */ }
      // KV 空或过期：从上游拉取并回填
      try {
        const fresh = await fetchAndCache(env);
        return new Response(fresh, {
          headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache" },
        });
      } catch (e) {
        // 上游也失败：回退打包的静态文件
        return env.ASSETS.fetch(request);
      }
    }
    return env.ASSETS.fetch(request);
  },

  // Cron 定时主动刷新 KV（wrangler.toml 的 [triggers].crons 控制频率）
  async scheduled(event, env) {
    try {
      await fetchAndCache(env);
    } catch (e) { /* 失败静默，下次 Cron 或请求时再试 */ }
  }
};
