// 前端静态资源由 ASSETS 托管；/data.json 改从 KV 读取（采集器云端写入），
// KV 为空时回退打包的静态 data.json，避免断层。
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/data.json") {
      try {
        const kv = await env.HOTNEWS_DATA.get("data.json");
        if (kv) {
          return new Response(kv, {
            headers: {
              "Content-Type": "application/json; charset=utf-8",
              "Cache-Control": "no-cache",
            },
          });
        }
      } catch (e) {
        // 读 KV 失败则回退静态
      }
      return env.ASSETS.fetch(request);
    }
    return env.ASSETS.fetch(request);
  }
};
