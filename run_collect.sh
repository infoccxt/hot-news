#!/bin/bash
# 热闻站本机定时采集：抓正文 -> GLM 摘要 -> 推送仓库（jsDelivr 自动分发）
set -u
cd /Users/fn/Vibe/think/hot-news || exit 1

# 本机 launchd 环境无系统代理、直连外网会 DNS 失败，需显式走本地代理出网。
# 自动探测本机可用代理（优先 WorkBuddy 本地代理，其次常见 Clash 端口）；
# 若都不可达则保持直连（适用于 TUN/透明代理环境）。
for _p in 127.0.0.1:61998 127.0.0.1:7890 127.0.0.1:7891 127.0.0.1:1087 127.0.0.1:8080; do
  if curl -s -o /dev/null -m 3 -x "http://$_p" https://api.github.com 2>/dev/null; then
    export HTTP_PROXY="http://$_p" HTTPS_PROXY="http://$_p" http_proxy="http://$_p" https_proxy="http://$_p"
    echo "  [proxy] 使用本机代理 $_p" >> "$LOG"
    break
  fi
done

PY=/Users/fn/.workbuddy/binaries/python/envs/default/bin/python
LOG=/Users/fn/Vibe/think/hot-news/logs/collect.log
mkdir -p "$(dirname "$LOG")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 开始定时采集 ===" >> "$LOG"
"$PY" collector.py >> "$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] collector 退出码=$?" >> "$LOG"

# 数据主路径：写入 Cloudflare KV（Worker 请求时从 KV 读，免每次 wrangler deploy）
# 同时 cp 一份到 public/data.json 作为 KV 空时的静态兜底 + 仓库留存。
cp -f data.json public/data.json
WRANGLER=/Users/fn/.npm-global/bin/wrangler
DATA_JSON="$(pwd)/data.json"
if [ -x "$WRANGLER" ]; then
  if "$WRANGLER" kv key put "data.json" --remote --binding HOTNEWS_DATA --path "$DATA_JSON" >> "$LOG" 2>&1; then
    echo "  [kv] 已写入 KV（线上从 KV 读最新数据）" >> "$LOG"
  else
    echo "  [kv] 写入失败（保留上次 KV 值 / 静态兜底，下次重试）" >> "$LOG"
  fi
  # 仅当 worker 代码或配置变动时才重新部署（数据已走 KV，无需每次重部署）
  if git diff --quiet HEAD -- worker.js wrangler.toml 2>/dev/null; then
    :
  else
    if "$WRANGLER" deploy >> "$LOG" 2>&1; then
      echo "  [cf] worker 代码变更，已重新部署" >> "$LOG"
    else
      echo "  [cf] deploy 失败（不影响数据 KV）" >> "$LOG"
    fi
  fi
else
  echo "  [cf] 未找到 wrangler，跳过 KV/部署" >> "$LOG"
fi

# 提交并推回仓库（站点经 jsDelivr 自动更新；CI 仅作手动兜底）
git pull --rebase --autostash origin main >> "$LOG" 2>&1 || echo "  [git] pull 失败，稍后重试" >> "$LOG"
git add data.json public/data.json cache/summaries.json >> "$LOG" 2>&1
if git diff --cached --quiet; then
  echo "  [git] 无变化，跳过提交" >> "$LOG"
else
  git commit -q -m "auto: 定时采集 $(date '+%m-%d %H:%M')" >> "$LOG" 2>&1
  if git push origin main >> "$LOG" 2>&1; then
    echo "  [git] 已推送 -> jsDelivr 更新" >> "$LOG"
  else
    echo "  [git] push 失败（可能需手动 pull）" >> "$LOG"
  fi
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 采集结束 ===" >> "$LOG"
