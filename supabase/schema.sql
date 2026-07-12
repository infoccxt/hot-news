-- 热闻聚合 · Supabase 表结构
-- 在 Supabase 控制台的 SQL Editor 里执行本文件即可。
-- 设计取舍：定时采集（每 30 分钟）全量覆盖，故用「先删后插」，无需唯一约束。

create table if not exists public.hot_items (
  id            bigint generated always as identity primary key,
  source        text      not null,   -- weibo / zhihu / github / hn-ai ...
  source_label  text      not null,   -- 微博 / GitHub 趋势 / AI 要闻 ...
  rank          int       not null,
  title         text      not null,
  url           text      default '',
  hot           bigint,
  desc          text,
  extra         jsonb     default '{}'::jsonb,
  fetched_at    timestamptz default now()
);

create index if not exists hot_items_source_idx on public.hot_items (source);
create index if not exists hot_items_fetched_idx on public.hot_items (fetched_at);

-- 公开只读：前端（任何域名）直接经 PostgREST 读取，无需密钥
alter table public.hot_items enable row level security;

drop policy if exists "public read hot_items" on public.hot_items;
create policy "public read hot_items"
  on public.hot_items for select
  using (true);

-- 写入仅限 service_role（Edge Function 使用），不开放匿名写
-- （Supabase 默认 service_role 绕过 RLS，无需额外 policy）
