#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热点聚合采集器
- 定时（每 30 分钟）抓取多个数据源，归一化为统一结构
- 输出到本地 data.json；云端定时（GitHub Actions）会把它提交回仓库，
  经 jsDelivr CDN 分发（全局加速、跨域开放、免费），前端直接读 -> 预览快
- 可选：同样写入 Supabase（见底部 supabase_flush，需要 SUPABASE 环境变量）

运行：python3 collector.py
依赖：标准库即可（无 OSS 依赖）
"""
import json, re, ssl, time, sys, datetime, os
import urllib.request
import concurrent.futures as cf

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# launchd 子进程直连 open.bigmodel.cn 会超时（DNS/路由受限），必须走脚本注入的代理；
# 代理出口在境外，智谱免费档按频率限流，靠缓存+低重试规避。git push 同样经代理。

# Python urllib 默认不读 HTTP_PROXY 环境变量；本机需经代理访问智谱，
# 故为 GLM 调用单独挂载代理 opener（抓取国内源的函数保持默认直连）。
_opener = None
_proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
          or os.environ.get("https_proxy") or os.environ.get("http_proxy"))
if _proxy:
    try:
        _opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": _proxy, "https": _proxy}))
        print(f"  [proxy] GLM 调用已挂载代理 {_proxy}")
    except Exception as _e:
        print("  [warn] 代理 opener 创建失败:", _e)


def _open(req, timeout):
    """发送 HTTP 请求：有代理则走代理 opener，否则直连（抓取国内源用）。"""
    if _opener:
        return _opener.open(req, timeout=timeout, context=CTX)
    return urllib.request.urlopen(req, timeout=timeout, context=CTX)


UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/html, */*"}

SIXTY = "https://60s.viki.moe/v2/{}"


def fetch(url, timeout=12, retries=3, as_json=True):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read().decode("utf-8", "ignore")
            return json.loads(raw) if as_json else raw
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    return None


def clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


# ── 中文翻译（GLM-4-Flash，免费）──────────────────────
def _glm_creds():
    """优先读 GLM_API_KEY 环境变量（CI/部署用），其次 EverOS .env（本地共用免费模型）。"""
    k = os.environ.get("GLM_API_KEY")
    base = os.environ.get("GLM_BASE_URL")
    if k and base:
        return k, base
    k = os.environ.get("EVEROS_LLM__API_KEY")
    base = os.environ.get("EVEROS_LLM__BASE_URL")
    if k and base:
        return k, base
    p = os.path.expanduser("~/.config/everos/.env")
    if os.path.exists(p):
        txt = open(p, encoding="utf-8").read()
        m = re.search(r"EVEROS_LLM__API_KEY\s*=\s*(\S+)", txt)
        n = re.search(r"EVEROS_LLM__BASE_URL\s*=\s*(\S+)", txt)
        if m:
            return m.group(1).strip(), (n.group(1).strip() if n else "https://open.bigmodel.cn/api/paas/v4")
    return None, None


def _extract_json(text):
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for a, b in (("[", "]"), ("{", "}")):
        s, e = text.find(a), text.rfind(b)
        if s != -1 and e != -1:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                pass
    return None


def _glm_chat(body, key, base, timeout=45):
    """用 curl 子进程调智谱：curl 自动读取环境代理（沙箱/本机均稳），
    比 urllib/requests 的代理处理更可靠。成功返回模型文本，失败返回 None。"""
    import subprocess
    url = base.rstrip("/") + "/chat/completions"
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X", "POST", url,
           "-H", "Authorization: Bearer " + key, "-H", "Content-Type: application/json",
           "-d", json.dumps(body, ensure_ascii=False)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        if r.returncode != 0:
            print("  [warn] GLM curl 失败 rc=%d %s" % (r.returncode, r.stderr[:120]))
            return None
        return json.loads(r.stdout)["choices"][0]["message"]["content"]
    except Exception as e:
        print("  [warn] GLM 请求失败:", e)
        return None


def glm_translate(texts, what="headlines"):
    """把一批英文文本翻译成中文，返回同序列表。任何失败都回退原文。"""
    if not texts:
        return texts
    key, base = _glm_creds()
    if not key:
        return texts
    if what == "headlines":
        sys_p = ("你是专业翻译。把下面提供的英文新闻标题逐条翻译成简体中文，"
                 "OpenAI、GPT、Google、Meta 等产品/公司名保留英文原文。"
                 "以 JSON 对象返回：{\"zh\":[\"译文1\",\"译文2\",...]}，"
                 "数组顺序与输入完全一致，不要任何额外文字或解释。")
    else:
        sys_p = ("你是专业翻译。把下面提供的英文软件项目简介逐条翻译成简体中文，"
                 "技术名词保留英文原文。"
                 "以 JSON 对象返回：{\"zh\":[\"译文1\",\"译文2\",...]}，"
                 "数组顺序与输入完全一致，不要任何额外文字或解释。")
    body = {
        "model": "GLM-4-Flash",
        "messages": [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ],
        "temperature": 0.3,
    }
    content = _glm_chat(body, key, base, 45)
    if content:
        obj = _extract_json(content)
        arr = None
        if isinstance(obj, list):
            arr = obj
        elif isinstance(obj, dict):
            arr = obj.get("zh") or obj.get("translations") or obj.get("result")
        if isinstance(arr, list) and len(arr) == len(texts):
            return [clean(x) or texts[i] for i, x in enumerate(arr)]
    return texts


# ── 摘要背景生成（GLM-4-Flash，免费）────────────────
SUMMARY_CACHE = os.path.join(os.path.dirname(__file__), "cache", "summaries.json")

def load_summary_cache():
    try:
        return json.load(open(SUMMARY_CACHE, encoding="utf-8"))
    except Exception:
        return {}

def save_summary_cache(cache):
    os.makedirs(os.path.dirname(SUMMARY_CACHE), exist_ok=True)
    json.dump(cache, open(SUMMARY_CACHE, "w", encoding="utf-8"), ensure_ascii=False)

def glm_summarize(titles):
    """给一批热搜话题生成一句话背景。失败回退空串。"""
    if not titles:
        return [""] * len(titles)
    key, base = _glm_creds()
    if not key:
        return [""] * len(titles)
    sys_p = ("你是热点背景助手。对下面每个热搜话题，用一句客观中文（不超过45字）说明它大致是什么、为何受关注。"
             "只基于普遍常识；若不清楚具体事件，只复述其字面含义或所属领域，"
             "严禁编造人名、数字、时间、事件细节。"
             "返回 JSON：{\"zh\":[\"背景1\",...]}，数组顺序与输入一致，不要任何额外文字。")
    body = {"model": "GLM-4-Flash",
            "messages": [{"role": "system", "content": sys_p},
                         {"role": "user", "content": json.dumps(titles, ensure_ascii=False)}],
            "temperature": 0.3}
    for attempt in range(3):
        content = _glm_chat(body, key, base, 45)
        if content:
            obj = _extract_json(content)
            arr = obj.get("zh") if isinstance(obj, dict) else (obj if isinstance(obj, list) else None)
            # 松弛接受：GLM 输出被截断导致数组长度不符时，按索引填充、缺失补空，
            # 避免整批丢弃（严格相等检查会让 194 条热词一次性全空）。
            if isinstance(arr, list) and arr:
                return [(clean(arr[i]) if i < len(arr) and clean(arr[i]) else "")
                        for i in range(len(titles))]
        print(f"  [warn] 摘要生成失败(第{attempt+1}次)")
        time.sleep(1.5)
    return [""] * len(titles)

# ── 正文抓取 + 真摘要（GLM-4-Flash，免费）──────────────
# 搜索/聚合页：没有单篇原文，只能做标题背景兜底
SEARCH_HINTS = ("s.weibo.com", "douyin.com/search", "xiaohongshu.com/search",
                "search_result", "/search?")

def is_search_url(u):
    return any(h in (u or "") for h in SEARCH_HINTS)

def fetch_extract(url, timeout=20, retries=2):
    """抓取网页并抽取正文，返回 (正文text或None, 错误信息)。"""
    import requests
    html = None
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
            if html and len(html) >= 500:
                break
            last = "short_html"
        except Exception as e:
            last = e
            time.sleep(1.0)
    if not html or len(html) < 500:
        return None, f"fetch:{last}"
    # 优先用 trafilatura 抽正文
    try:
        import trafilatura
        txt = trafilatura.extract(html, url=url, include_comments=False,
                                  include_tables=False)
    except Exception:
        txt = None
    if not txt or len(txt) < 120:
        txt = _fallback_extract(html)
    if not txt or len(txt) < 120:
        return None, "no_text"
    return txt[:6000], None

def _fallback_extract(html):
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    blocks = re.findall(r"<(p|article)[^>]*>(.*?)</\1>", h, re.S | re.I)
    out = []
    for _, body in blocks:
        t = clean(re.sub(r"<[^>]+>", "", body))
        if len(t) > 30:
            out.append(t)
    return "\n".join(out)[:6000]

def glm_summarize_text(texts, max_chars=110):
    """对一批网页正文片段生成中文一句话摘要。失败回退空串。"""
    if not texts:
        return [""] * len(texts)
    key, base = _glm_creds()
    if not key:
        return [""] * len(texts)
    sys_p = ("你是新闻摘要助手。下面每条是一篇网页文章的正文片段。"
             "请用一句简体中文（不超过{}字）客观概括其核心内容，"
             "只依据给定正文，严禁编造人名、数字、时间、结论；"
             "若正文信息不足，只说其讨论的主题领域。"
             "返回 JSON：{{\"zh\":[\"摘要1\",...]}}，数组顺序与输入一致，不要任何额外文字。"
             ).format(max_chars)
    body = {"model": "GLM-4-Flash",
            "messages": [{"role": "system", "content": sys_p},
                         {"role": "user", "content": json.dumps(texts, ensure_ascii=False)}],
            "temperature": 0.3}
    for attempt in range(3):
        content = _glm_chat(body, key, base, 60)
        if content:
            obj = _extract_json(content)
            arr = obj.get("zh") if isinstance(obj, dict) else (obj if isinstance(obj, list) else None)
            if isinstance(arr, list) and arr:
                return [(clean(arr[i]) if i < len(arr) and clean(arr[i]) else "")
                        for i in range(len(texts))]
        print(f"  [warn] 正文摘要失败(第{attempt+1}次)")
        time.sleep(1.5)
    return [""] * len(texts)

def enrich(sources, max_workers=8):
    """抓取有正文的条目做真摘要；搜索页/无链接条目回退标题背景。
    带 url 缓存：有内容不重抓；首次运行用线程池并发抓 ~16 路，正文批量丢给 GLM 摘要。"""
    cache = load_summary_cache()
    tasks = []  # (kind, si, ii, url)
    for si, s in enumerate(sources):
        for ii, it in enumerate(s["items"]):
            url = it.get("url")
            # 源自带真实 desc（科技资讯/ GitHub 简介/ HN 正文片段等）→ 保留，不抓
            if it.get("desc"):
                it["desc_source"] = "source"
                if url:
                    cache[url] = it["desc"]
                continue
            if not url:
                continue  # 无链接（每日早报）→ 走标题背景兜底
            if is_search_url(url):
                continue  # 搜索页无单篇原文，直接走标题背景兜底
            tasks.append(("article", si, ii, url))

    # 并发抓正文（慢在网络 I/O，线程池提速）
    fetched = {}
    def do_fetch(t):
        return t, fetch_extract(t[3])
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for t, (txt, err) in ex.map(do_fetch, tasks):
            fetched[t] = (txt, err)

    # 收集可摘要的正文，批量调 GLM（限流：每批 8 条，匹配免费档 QPS≈20）
    to_summ, results = [], {}
    for t, (txt, err) in fetched.items():
        if txt:
            to_summ.append((t, txt))
    B = 8
    for i in range(0, len(to_summ), B):
        batch = to_summ[i:i + B]
        sms = glm_summarize_text([x[1] for x in batch])
        for (t, _), sm in zip(batch, sms):
            results[t] = sm

    for t, sm in results.items():
        if sm:
            _, si, ii, url = t
            sources[si]["items"][ii]["desc"] = sm
            sources[si]["items"][ii]["desc_source"] = "fetch"
            cache[url] = sm

    # 兜底：仍无 desc 的条目用标题背景（搜索页/抓取失败/无链接）
    need = []
    for si, s in enumerate(sources):
        for ii, it in enumerate(s["items"]):
            if not it.get("desc") and it.get("title"):
                need.append((si, ii, it["title"]))
    if need:
        titles = [x[2] for x in need]
        bg = []
        G = 15  # 一批最多 15 条，避免 GLM 输出截断；免费档按 IP 限频，批间留间隔
        for i in range(0, len(titles), G):
            chunk = titles[i:i + G]
            bg.extend(glm_summarize(chunk))
            if i + G < len(titles):
                time.sleep(1.5)
        for (si, ii, _), b in zip(need, bg):
            if b:
                it = sources[si]["items"][ii]
                it["desc"] = b
                it["desc_source"] = "guess"
                key = it.get("url") or ("t:" + it["title"])
                cache[key] = b
    save_summary_cache(cache)
    f = sum(1 for s in sources for it in s["items"] if it.get("desc_source") == "fetch")
    g = sum(1 for s in sources for it in s["items"] if it.get("desc_source") == "guess")
    src = sum(1 for s in sources for it in s["items"] if it.get("desc_source") == "source")
    print(f"  [摘要] 真抓原文={f}  源自带={src}  标题背景={g}")


def source_60s(key, label, path, item_map, limit=50):
    """通用 60s 列表端点。item_map 负责把原始 item 转成统一结构。"""
    d = fetch(SIXTY.format(path))
    if not d or d.get("code") != 200:
        return None
    data = d.get("data")
    items = data if isinstance(data, list) else data.get("list") if isinstance(data, dict) else None
    if not items:
        return None
    out = []
    for i, it in enumerate(items[:limit], 1):
        m = item_map(it, i)
        if m:
            out.append(m)
    return {"key": key, "label": label, "count": len(out), "items": out}


def map_hot(it, i):
    title = clean(it.get("title"))
    if not title:
        return None
    desc = clean(it.get("description") or it.get("detail") or "")
    return {"title": title, "url": it.get("link") or it.get("url") or "",
            "hot": it.get("hot_value"), "rank": i, "desc": desc or None, "extra": {}}


def map_rednote(it, i):
    title = clean(it.get("title"))
    if not title:
        return None
    return {"title": title, "url": it.get("link") or "", "hot": it.get("score"),
            "rank": i, "desc": None, "extra": {}}


def map_60s_news(it, i):
    title = clean(it)
    if not title:
        return None
    return {"title": title, "url": "", "hot": None, "rank": i, "desc": None, "extra": {}}


def source_github_trending(limit=25):
    html = None
    for _ in range(3):
        html = fetch("https://github.com/trending?since=daily", as_json=False, retries=4)
        if html:
            break
    if not html:
        return None
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.S)
    out = []
    for i, art in enumerate(articles[:limit], 1):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="(/[^"]+)"', art)
        if not m:
            continue
        repo = m.group(1).strip("/")
        desc = re.search(r'<p[^>]*class="col-9[^"]*"[^>]*>(.*?)</p>', art, re.S)
        desc = clean(re.sub(r"<[^>]+>", "", desc.group(1))) if desc else ""
        lang = re.search(r'<span itemprop="programmingLanguage">(.*?)</span>', art)
        lang = clean(lang.group(1)) if lang else ""
        stars = re.findall(r'<a[^>]*href="/{}/stargazers"[^>]*>(.*?)</a>'.format(repo), art, re.S)
        stars_total = clean(re.sub(r"<[^>]+>", "", stars[0])) if stars else ""
        today = re.search(r'([\d,]+)\s+stars today', art)
        today_n = clean(today.group(1)) if today else ""
        out.append({
            "title": repo, "url": "https://github.com/" + repo,
            "hot": int(today_n.replace(",", "")) if today_n else None,
            "rank": i, "desc": desc,
            "extra": {"language": lang, "stars": stars_total, "stars_today": today_n},
        })
    # 入库时把英文项目简介翻译成中文（仓库名保留原文）
    if out:
        idxs, texts = [], []
        for i, it in enumerate(out):
            if it.get("desc"):
                idxs.append(i)
                texts.append(it["desc"])
        if texts:
            for i, t in zip(idxs, glm_translate(texts, "desc")):
                out[i]["desc"] = t
    return {"key": "github", "label": "GitHub 趋势", "count": len(out), "items": out}


def source_hn_ai(limit=25):
    # 用 Algolia API 获取高亮摘要（highlightResult）
    d = fetch("https://hn.algolia.com/api/v1/search?query=AI&tags=story&hitsPerPage={}&attributesToRetrieve=title,url,points,objectID,author,num_comments&attributesToHighlight=story_text".format(limit))
    if not d:
        return None
    hits = d.get("hits", [])
    out = []
    hr_all = d.get("highlightResult") or {}
    for i, h in enumerate(hits, 1):
        title = clean(h.get("title"))
        if not title:
            continue
        # 从 highlightResult 提取正文片段
        desc = ""
        oid = str(h.get("objectID", ""))
        hr = hr_all.get(oid, {})
        story_hr = hr.get("story_text", {})
        if isinstance(story_hr, dict):
            val = story_hr.get("value", "")
            if val:
                # 去掉 HTML 标签
                desc = clean(re.sub(r"<[^>]+>", "", val))[:300]
        out.append({
            "title": title,
            "url": h.get("url") or ("https://news.ycombinator.com/item?id=" + oid),
            "hot": h.get("points"),
            "rank": i, "desc": desc or None,
            "extra": {"author": h.get("author", ""), "comments": h.get("num_comments", 0)},
        })
    # 入库时统一翻译成中文（免费 GLM-4-Flash）
    if out:
        titles = [it["title"] for it in out]
        zh = glm_translate(titles, "headlines")
        for it, t in zip(out, zh):
            it["title"] = t
    return {"key": "hn-ai", "label": "AI 要闻", "count": len(out), "items": out}


def load_prev():
    p = os.path.join(os.path.dirname(__file__), "data.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        return {s["key"]: s for s in d.get("sources", [])}
    except Exception:
        return {}


def collect():
    prev = load_prev()
    collected = {}

    def accept(s):
        if s:
            collected[s["key"]] = s
            return True
        return False

    def reuse(key):
        if key in prev:
            collected[key] = prev[key]
            print(f"  [复用] {prev[key]['label']}: {prev[key]['count']} 条（本次抓取失败）")
            return True
        return False

    # HN AI 要闻（置顶第一位）
    try:
        h = source_hn_ai(25)
        if accept(h):
            print(f"  [ok] AI 要闻: {h['count']} 条")
        elif not reuse("hn-ai"):
            print(f"  [skip] AI 要闻: 无数据")
    except Exception as e:
        if not reuse("hn-ai"):
            print(f"  [err] AI 要闻: {e}")

    specs = [
        ("weibo", "微博", "weibo", map_hot, 50),
        ("zhihu", "知乎", "zhihu", map_hot, 50),
        ("douyin", "抖音", "douyin", map_hot, 50),
        ("toutiao", "头条", "toutiao", map_hot, 50),
        ("rednote", "小红书", "rednote", map_rednote, 30),
        ("it-news", "科技资讯", "it-news", map_hot, 40),
    ]
    for key, label, path, mp, lim in specs:
        try:
            s = source_60s(key, label, path, mp, lim)
            if accept(s):
                print(f"  [ok] {label}: {s['count']} 条")
            elif not reuse(key):
                print(f"  [skip] {label}: 无数据")
        except Exception as e:
            if not reuse(key):
                print(f"  [err] {label}: {e}")

    # 60s 每日早报
    try:
        d = fetch(SIXTY.format("60s"))
        if d and d.get("code") == 200 and isinstance(d.get("data"), dict):
            news = d["data"].get("news", [])
            items = [map_60s_news(n, i) for i, n in enumerate(news[:30], 1)]
            items = [x for x in items if x]
            if accept({"key": "60s", "label": "每日早报", "count": len(items), "items": items}):
                print(f"  [ok] 每日早报: {len(items)} 条")
            elif not reuse("60s"):
                print(f"  [skip] 每日早报: 无数据")
    except Exception as e:
        if not reuse("60s"):
            print(f"  [err] 每日早报: {e}")

    # GitHub 趋势
    try:
        g = source_github_trending(25)
        if accept(g):
            print(f"  [ok] GitHub 趋势: {g['count']} 条")
        elif not reuse("github"):
            print(f"  [skip] GitHub 趋势: 无数据")
    except Exception as e:
        if not reuse("github"):
            print(f"  [err] GitHub 趋势: {e}")

    # 固定展示顺序：AI 要闻置顶
    order = ["hn-ai", "weibo", "zhihu", "douyin", "toutiao",
             "rednote", "it-news", "60s", "github"]
    sources = [collected[k] for k in order if k in collected]

    # 抓正文做真摘要（有源自带 desc / 有缓存则跳过，不重抓）
    enrich(sources)

    return {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sources": sources,
    }


def oss_flush(payload):
    import oss2
    # 清代理（沙箱/CI 环境都可能被代理干扰）
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)

    # 优先从环境变量读取（GitHub Actions / CI），其次读本地 picgo 配置
    key_id = os.environ.get("ALIYUN_ACCESS_KEY_ID")
    key_sec = os.environ.get("ALIYUN_ACCESS_KEY_SECRET")
    bucket_name = os.environ.get("ALIYUN_OSS_BUCKET")
    region = os.environ.get("ALIYUN_OSS_REGION")

    if not (key_id and key_sec and bucket_name and region):
        # 本地模式：从 picgo 配置读取
        cfg_path = os.path.expanduser("~/Library/Application Support/picgo/data.json")
        if not os.path.exists(cfg_path):
            print("  [err] 无 OSS 凭证：未设置环境变量，也未找到本地 picgo 配置")
            return None
        cfg = json.load(open(cfg_path))
        a = cfg["picBed"]["aliyun"]
        key_id, key_sec, bucket_name, region = a["accessKeyId"], a["accessKeySecret"], a["bucket"], a["area"]

    auth = oss2.Auth(key_id, key_sec)
    endpoint = f"https://{region}.aliyuncs.com"
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    body = json.dumps(payload, ensure_ascii=False)
    # 公开读，前端可直接拉取
    bucket.put_object(OSS_OBJECT, body,
                      headers={"Content-Type": "application/json; charset=utf-8",
                               "x-oss-object-acl": "public-read"})
    # 配置 CORS：允许任意来源 GET（前端在不同域名下读取）
    rule = oss2.models.CorsRule(allowed_origins=["*"], allowed_methods=["GET"],
                                allowed_headers=["*"], max_age_seconds=300)
    try:
        bucket.put_bucket_cors(oss2.models.BucketCors([rule]))
    except Exception as e:
        print("  [warn] CORS 设置失败（可能已存在）:", e)
    url = f"https://{bucket_name}.{region}.aliyuncs.com/{OSS_OBJECT}"
    print(f"  [oss] 已上传 -> {url}  ({len(body)} bytes)")
    return url


def supabase_flush(payload):
    """可选：若设置了 SUPABASE_URL / SUPABASE_KEY，则同时写入 Supabase。"""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not (url and key):
        return
    import urllib.request
    # 简单实现：把每个 source 的 items 写入 hot_items 表（upsert by source+rank）
    rows = []
    for s in payload["sources"]:
        for it in s["items"]:
            rows.append({
                "source": s["key"], "source_label": s["label"], "rank": it["rank"],
                "title": it["title"], "url": it.get("url", ""), "hot": it.get("hot"),
                "desc": it.get("desc"), "extra": json.dumps(it.get("extra", {}), ensure_ascii=False),
                "fetched_at": payload["updated_at"],
            })
    data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{url}/rest/v1/hot_items", data=data, method="POST",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json",
                                          "Prefer": "resolution=merge-duplicates"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            print(f"  [supabase] 写入 {len(rows)} 行, status={r.status}")
    except Exception as e:
        print("  [supabase] 写入失败:", e)


if __name__ == "__main__":
    print("== 开始采集 ==", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    payload = collect()
    total = sum(s["count"] for s in payload["sources"])
    print(f"== 共 {len(payload['sources'])} 个源, {total} 条 ==")

    # 数据落地：写本地 data.json（不再写入 OSS，避免污染 Obsidian 同步桶）
    # 云端定时（GitHub Actions）会把这个文件提交回仓库，经 jsDelivr CDN 分发
    out_path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"== 完成 == 本地数据: {out_path} ({os.path.getsize(out_path)} bytes)")

    # 可选：若设置了 SUPABASE_URL / SUPABASE_KEY，同时写入 Supabase
    supabase_flush(payload)
