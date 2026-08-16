---
name: adaptive-search
description: "Use this as Codex's default external search, replacing native Web Search with a local, non-MCP multi-provider router for current information, technical research, comparisons, semantic discovery, news, Chinese web content, GitHub searches, and source-backed answers. Use conditional second searches, bounded task-level budgets, Trafilatura-backed query-relevant compact page extraction, and limited Firecrawl/Tavily/Exa verification for core claims or conflicts."
---

# Adaptive Search

Use this Skill as the default external-search path for Codex. Whenever a task
needs current, changing, niche, source-backed, news, Chinese-web, technical,
or GitHub information, use the bundled CLI instead of Codex native Web Search.
The CLI calls providers directly over HTTP; it does not require MCP or a
resident server.

## Workflow

1. Decide whether external information is needed.
2. Set intent to auto unless the task clearly specifies one.
3. Set depth to auto unless the user requests a depth:
   - quick: one provider first; call one different provider only when the
     result is sparse, weak, domain-concentrated, or the user asks for more.
   - balanced: one provider first; call a second provider when coverage,
     freshness, source quality, or independence is insufficient.
   - deep: use at least two and at most three complementary providers unless
     the caller explicitly selects one provider; use the third only for gaps
     or conflicts.
4. Run scripts/search.py with `--task-query` set to the original user
   question. Reuse the exact same `--task-query` for every follow-up. The
   router automatically scopes the ledger to `CODEX_THREAD_ID` in Codex; use
   `--task-id` only for manual callers that need an explicit scope. Inspect
   `quality` and `follow_up`. If
   `follow_up.recommended` is true, let the router perform the next provider
   search. Use `--force-followup` when the user explicitly says the result is
   not satisfactory even if the objective quality gate passed.
5. Treat search as the discovery and cross-check phase. Do not use page
   extraction as a substitute for a second search when candidates, coverage,
   freshness, or independent evidence are missing.
6. After the search set is stable, select only the one to four URLs needed to
   verify core claims, resolve a conflict, or read exact wording. Pass them
   in one bounded scripts/fetch.py command with the original query.
7. Use the returned normalized sources and extracted evidence for the answer.
   Do not repeatedly fetch every candidate page or manually merge raw provider
   responses in the model context.

The router does not preflight quotas or call usage APIs on the search path.
It uses static routing, objective quality gates, response errors, local
caching, and bounded fallback. Do not manufacture requests to balance usage.

## Intent routing

Use these intent meanings:

- web: general web search, official pages, explicit facts.
- semantic: concepts, long-tail material, similar content.
- research: technical research, papers, comparisons, multi-source evidence.
- news: recent events and news reporting.
- cn: China-mainland web, Chinese domestic policy, local information.
- code: GitHub repositories, issues, pull requests, users, and code.
- google: an explicit Google SERP requirement; reserve Serper for this.

The default provider roles are:

- General web: Brave, then Exa, then Tavily.
- Semantic/research: Exa, then Tavily, then Brave.
- Global news: NewsAPI/Tavily, then Brave, then Exa.
- China news or policy: Baidu, then NewsAPI/Tavily, then Brave.
- GitHub: GitHub API, then Brave/Exa only when GitHub data is insufficient.
- Page extraction: one local HTTP download followed by in-memory Trafilatura
  extraction first, then the built-in heuristic fallback, Tavily Extract or
  Exa Contents, with Firecrawl reserved as the final advanced fallback.

For low-risk general web queries, the router rotates the first provider
with a small fixed preference of Brave:Exa:Tavily = 2:1:1. Do not rotate away
from a specialist provider for research, news, cn, or code tasks.

## Staged search rules

The arrow order is a preference chain, not a requirement to call every
provider. The router exposes objective quality signals after every round:

- `no_results` or `sparse_results`: search again.
- `low_domain_diversity`: search a different provider.
- `weak_snippets` or `missing_freshness`: search again before fetching.
- `independent_search_needed`: deep requires a second provider by default;
  balanced requires one for comparisons, research, and current claims.

The provider caps are:

- quick: at most two providers, with the second conditional.
- balanced: at most two providers, with the second conditional.
- deep: at least two and at most three providers by default; use the third
  only when the first two leave a gap or conflict. An explicit `--provider`
  is a deliberate single-source override.

For quick and balanced, stop without a second search when the first result set
is objectively sufficient. For deep, do not stop after the first provider
unless the caller explicitly selected one provider or no alternate provider is
available. If the user says a quick or balanced result is not sufficient, use
`--force-followup` once.
For a follow-up in the same task, the router prefers providers that have not
yet been used. A repeated query on the same provider is not an independent
search view; reuse it only when every suitable provider has already been used
or the caller explicitly selected that provider. Keep Firecrawl out of
discovery rounds.

## Task-level search budget

Treat one user question as one search task, even when the search wording is
rewritten. Pass the original question with `--task-query` on every search
command; the CLI requires it for non-status searches. The router keeps a
24-hour ledger for telemetry, cache-aware provider rotation, and diagnostics;
it is advisory and never blocks the first search of a new task or session.

Execution is bounded by the current invocation's depth instead:

- quick: at most two provider rounds, with the second conditional;
- balanced: at most two provider rounds, with the second conditional;
- deep: at most three provider rounds, with the second used for independent
  evidence and the third only for a gap or conflict.

Do not fan out one question into several independent `search.py` commands.
Use one initial query and one targeted follow-up only when `quality` or
`follow_up` requires it. Do not change the query wording to evade a limit;
the limit is the per-invocation round cap, not a cross-session lock. Omitting
`--task-query` is still an error. The output `task.providers_used` shows
provider diversity within the current Codex thread.

## Command examples

Run these from the skill directory, or replace scripts/ with its absolute
path:

~~~bash
python3 scripts/search.py --task-query "latest Python 3.14 changes" \
  --query "latest Python 3.14 changes" --depth auto
python3 scripts/search.py --task-query "WebAssembly Component Model and WASI" \
  --query "WebAssembly Component Model and WASI" --intent research --depth deep
python3 scripts/search.py --task-query "中国人工智能监管政策最新进展" \
  --query "中国人工智能监管政策最新进展" --intent cn --depth balanced
python3 scripts/search.py --task-query "owner/repo issue search" \
  --query "owner/repo issue search" --intent code --depth quick
python3 scripts/search.py --task-query "same question" \
  --query "same question" --depth quick --force-followup
python3 scripts/fetch.py --query "same question" --url "https://example.com/article" \
  --provider auto --max-chars 8000 --context-budget 8000
python3 scripts/fetch.py --query "comparison question" --max-pages 3 \
  --max-chars 8000 --context-budget 20000 \
  --url "https://example.com/ranking" \
  --url "https://example.com/official-model-page" \
  --url "https://example.com/independent-benchmark"
python3 scripts/search.py --status
python3 scripts/search.py --query "exact Google SERP request" --intent google --allow-reserve
~~~

The scripts emit JSON on stdout. Treat provider errors and `follow_up` signals
as routing information, not as reasons to expose API keys or raw authentication
headers.

## Page verification rules

Use page extraction only after discovery and cross-search are complete. The
fetcher performs extractive compaction: it preserves the lead/headings and
selects blocks relevant to the original question instead of returning the
first N characters blindly. It also enforces a combined context budget for a
batch.

If a long page has no lexical match for the query, the fetcher must not return
the full page as a fallback. It returns a small bounded overview instead:
identity/lead, a few headings, and evenly spaced body samples, capped at the
smaller of one third of the page allowance or 2,400 characters. Pages whose
extracted text is only a loading placeholder (for example, "加载中..." or
"Loading...") are extraction failures, not evidence. In `auto` mode the router
may try the next extractor; browser-grade extraction remains an advanced
fallback for genuinely client-rendered pages.

- quick: normally zero pages; at most one for exact wording.
- balanced: at most two core pages.
- deep: normally two or three core pages; use four only for a genuine
  multi-candidate comparison.

Use approximately 6,000–8,000 output characters per page and keep the combined
batch under 8,000 for quick, 12,000–16,000 for balanced, and 20,000–24,000
for deep. These are character budgets, not token budgets; reduce them for
Chinese-heavy pages, code, or tables. Pass the original question so Tavily
and the local relevance filter can focus the returned content.

Pass the original search question with `--query`. The fetcher canonicalizes
URLs, deduplicates repeated URLs, caches compacted content locally for 6 hours,
and enforces the per-invocation page limit and batch context budget.
There is no historical per-question fetch budget: `--max-pages` (or its
deprecated `--fetch-budget` alias) only limits the current invocation.
Fetch a leaderboard or independent benchmark first, then only the leading
candidates' official pages. If pages conflict, return to search with a
targeted query instead of fetching many more pages. Use `--provider firecrawl`
only when a page needs browser-grade or advanced extraction.

Never use Codex native Web Search or `web__run` as a fallback for this Skill.
If the local router returns an error, report the router error or use its own
next-provider round; do not switch search systems behind the user's back.

The local path downloads a page once and passes the HTML directly to
Trafilatura; it does not call `trafilatura.fetch_url`, save raw HTML, or make a
second request. Trafilatura emits compact Markdown with comments removed,
tables retained, links omitted, and duplicate content reduced. If the package
is unavailable or extraction fails, the built-in parser remains available and
the router can then fall back to Tavily, Exa, or Firecrawl. Dynamic-shell
rejections are recorded as provider errors so a placeholder is never cached as
valid page evidence. Install the local
extractor with `python3 -m pip install --user --break-system-packages -r
requirements.txt` when setting up a new machine.

## Environment variables

The scripts read keys from environment variables and, when present, the
user-private `~/.config/searchx/secrets.env` file. Process environment
variables take precedence. Keep that secrets file outside the skill and
workspace with user-only permissions:

- BRAVE_API_KEY
- EXA_API_KEY
- TAVILY_API_KEY
- BAIDU_QIANFAN_API_KEY or BAIDU_API_KEY
- NEWS_API_KEY
- GITHUB_API_KEY or GITHUB_TOKEN
- FIRECRAWL_API_KEY
- SERPER_API_KEY
- SEARXNG_URL (optional fallback, for example http://127.0.0.1:8080)

Providers with missing keys are skipped. Never write key values into this
skill, its scripts, the workspace, or a Git repository.

## Output handling

The router returns normalized fields including provider, title, url, snippet,
published_at, domain, rank, and source_kind. Prefer official or primary
sources for exact technical and policy claims. Preserve the returned URLs as
citations in the final answer.

Do not treat provider-native scores as comparable across providers. Use the
router's deduplicated ordering, source diversity, freshness, and source
quality instead.
