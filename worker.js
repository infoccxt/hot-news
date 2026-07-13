// 数据主源已改为阿里云 OSS（浏览器在中国直连，绕过 Cloudflare 出境 fetch 到 GitHub/jsDelivr 的限制）。
// 本 Worker 只负责托管静态前端；/data.json 兜底返回打包内置的快照（平时前端直接读 OSS，此路由仅应急）。
// 不再依赖 KV / Cron / CF 出站拉取 —— 采集与部署完全解耦，Actions 用 GITHUB_TOKEN 提交、用 OSS key 上传。
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/data.json") {
      // 打包内置快照兜底：若 public/ 下无 data.json 则返回 404，前端会自动转 jsDelivr。
      return env.ASSETS.fetch(request);
    }
    return env.ASSETS.fetch(request);
  }
};
