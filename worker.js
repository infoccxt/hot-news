// 静态资源托管：前端直接连 Hacker News 官方 API（CORS 友好，无需后端转发）
export default {
  async fetch(request, env) {
    return env.ASSETS.fetch(request);
  }
};
