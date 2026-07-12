// Supabase Edge Function —— 定时采集热点并写入 hot_items 表
//
// 部署：
//   supabase functions deploy collect --no-verify-jwt
//   supabase secrets set SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...
//
// 每 30 分钟触发（在 Supabase SQL Editor 执行，需先 enable pg_cron）：
//   select cron.schedule(
//     'collect-hot',
//     '30 minutes',
//     $$ select net.http_post(
//          url:='https://<你的项目>.functions.supabase.co/collect',
//          headers:='{"Authorization":"Bearer <你的ANON_KEY>"}'::jsonb) $$
//   );
//
// 前端切换到 Supabase：把 index.html 里的 DATA_URL 改为
//   `${SUPABASE_URL}/rest/v1/hot_items?select=source,source_label,rank,title,url,hot,desc,extra&order=source.asc,rank.asc`
// 并把返回的扁平数组按 source 字段重新分组为 sources[] 即可（逻辑已就绪，给到 key 后我来完成这步）。

const SIXTY = "https://60s.viki.moe/v2/";

async function get(url: string, asJson = true, retries = 3): Promise<any> {
  for (let i = 0; i < retries; i++) {
    try {
      const r = await fetch(url, {
        headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json,*/*" },
      });
      const txt = await r.text();
      return asJson ? JSON.parse(txt) : txt;
    } catch (e) {
      await new Promise((r) => setTimeout(r, 700 * (i + 1)));
    }
  }
  return null;
}

function clean(s: any): string {
  return s ? String(s).replace(/\s+/g, " ").trim() : "";
}

async function src60(path: string, key: string, label: string, limit = 50) {
  const d = await get(SIXTY + path);
  if (!d || d.code !== 200) return null;
  const items = Array.isArray(d.data) ? d.data : [];
  const out = items.slice(0, limit).map((it: any, i: number) => ({
    source: key, source_label: label, rank: i + 1,
    title: clean(it.title),
    url: it.link || it.url || "",
    hot: it.hot_value ?? null,
    desc: clean(it.description || it.detail || "") || null,
    extra: {},
  })).filter((x: any) => x.title);
  return out.length ? out : null;
}

async function githubTrending(limit = 25) {
  let html: string | null = null;
  for (let i = 0; i < 3 && !html; i++) html = await get("https://github.com/trending?since=daily", false, 4);
  if (!html) return null;
  const arts = [...html.matchAll(/<article class="Box-row">([\s\S]*?)<\/article>/g)];
  const out: any[] = [];
  arts.slice(0, limit).forEach((m, i) => {
    const a = m[1];
    const repo = a.match(/<h2[^>]*>\s*<a[^>]*href="\/([^"]+)"/)?.[1]?.trim();
    if (!repo) return;
    const desc = clean(a.match(/<p[^>]*class="col-9[^"]*"[^>]*>([\s\S]*?)<\/p>/)?.[1]?.replace(/<[^>]+>/g, ""));
    const lang = clean(a.match(/<span itemprop="programmingLanguage">(.*?)<\/span>/)?.[1]);
    const stars = clean(a.match(new RegExp(`<a[^>]*href="/${repo}/stargazers"[^>]*>([\s\S]*?)</a>`))?.[1]?.replace(/<[^>]+>/g, ""));
    const today = a.match(/([\d,]+)\s+stars today/)?.[1];
    out.push({
      source: "github", source_label: "GitHub 趋势", rank: i + 1,
      title: repo, url: "https://github.com/" + repo,
      hot: today ? Number(today.replace(/,/g, "")) : null,
      desc: desc || null,
      extra: { language: lang, stars },
    });
  });
  return out.length ? out : null;
}

async function hnAi(limit = 25) {
  const d = await get(`https://hn.algolia.com/api/v1/search?query=AI&tags=story&hitsPerPage=${limit}`);
  if (!d) return null;
  const out = (d.hits || [])
    .map((h: any, i: number) => ({
      source: "hn-ai", source_label: "AI 要闻", rank: i + 1,
      title: clean(h.title),
      url: h.url || `https://news.ycombinator.com/item?id=${h.objectID}`,
      hot: h.points ?? null,
      desc: null,
      extra: { author: h.author, comments: h.num_comments },
    }))
    .filter((x: any) => x.title);
  return out.length ? out : null;
}

async function collect() {
  const rows: any[] = [];
  const specs: [string, string, string, number][] = [
    ["weibo", "微博", "weibo", 50], ["zhihu", "知乎", "zhihu", 50],
    ["douyin", "抖音", "douyin", 50], ["toutiao", "头条", "toutiao", 50],
    ["rednote", "小红书", "rednote", 30], ["it-news", "科技资讯", "it-news", 40],
  ];
  for (const [key, label, path, lim] of specs) {
    const s = await src60(path, key, label, lim);
    if (s) rows.push(...s);
  }
  const d60 = await get(SIXTY + "60s");
  if (d60?.data?.news) {
    const items = (d60.data.news as string[]).slice(0, 30).map((n: string, i: number) => ({
      source: "60s", source_label: "每日早报", rank: i + 1,
      title: clean(n), url: "", hot: null, desc: null, extra: {},
    }));
    rows.push(...items);
  }
  const g = await githubTrending(25); if (g) rows.push(...g);
  const h = await hnAi(25); if (h) rows.push(...h);
  return rows;
}

async function flush(rows: any[]) {
  const url = `${Deno.env.get("SUPABASE_URL")}/rest/v1/hot_items`;
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const headers = { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
  // 全量覆盖：先清空，再插入
  await fetch(url + "?on_conflict=id", { method: "DELETE", headers });
  await fetch(url, {
    method: "POST",
    headers: { ...headers, Prefer: "return=minimal" },
    body: JSON.stringify(rows),
  });
}

serve(async () => {
  try {
    const rows = await collect();
    await flush(rows);
    return new Response(`ok: ${rows.length} rows`, { status: 200 });
  } catch (e) {
    return new Response("err: " + (e as Error).message, { status: 500 });
  }
});
