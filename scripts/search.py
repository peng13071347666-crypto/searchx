#!/usr/bin/env python3
"""A small, non-MCP, staged multi-provider search router."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


PROVIDER_ENV = {
    "brave": ("BRAVE_API_KEY",),
    "exa": ("EXA_API_KEY",),
    "tavily": ("TAVILY_API_KEY",),
    "baidu": ("BAIDU_QIANFAN_API_KEY", "BAIDU_API_KEY"),
    "newsapi": ("NEWS_API_KEY", "NEWSAPI_API_KEY"),
    "github": ("GITHUB_API_KEY", "GITHUB_TOKEN"),
    "firecrawl": ("FIRECRAWL_API_KEY",),
    "serper": ("SERPER_API_KEY",),
}

DEPTH_LIMITS = {"quick": 2, "balanced": 2, "deep": 3}
RESULT_LIMITS = {"quick": 5, "balanced": 8, "deep": 12}
TASK_PROVIDER_BUDGETS = {"quick": 2, "balanced": 2, "deep": 3}
TASK_BUDGET_TTL = 24 * 60 * 60
TASK_KEY_VERSION = "thread-v2"

NEWS_WORDS = (
    "news", "latest", "today", "recent", "breaking", "新闻", "时事",
    "最新", "近期", "刚刚", "动态", "进展",
)
RESEARCH_WORDS = (
    "research", "compare", "comparison", "survey", "review", "benchmark",
    "paper", "论文", "调研", "研究", "比较", "对比", "评估", "全面",
    "系统分析", "优缺点", "方案",
)
COMPARISON_WORDS = (
    "best", "strongest", "top", "which is better", "which one",
    "最强", "最好", "最优", "哪个更好", "哪一个", "排名", "排行",
)
SEMANTIC_WORDS = (
    "what is", "how does", "similar", "related", "概念", "原理", "区别",
    "为什么", "如何理解", "相似", "长尾",
)
CN_WORDS = (
    "中国", "国内", "大陆", "北京", "上海", "深圳", "政策", "监管",
    "百度", "工信部", "网信办", "国务院",
)
CODE_WORDS = (
    "github", "repo", "repository", "issue", "pull request", "pull_request",
    "pr ", "代码", "仓库", "提交", "release",
)
GLOBAL_WORDS = (
    "openai", "github", "python", "webassembly", "wasi", "google",
    "brave", "exa", "tavily", "firecrawl", "searxng",
)


class ProviderError(Exception):
    def __init__(self, provider: str, status: int | None, message: str):
        self.provider = provider
        self.status = status
        self.message = message
        super().__init__(message)


def load_secret_env() -> None:
    """Load a private key=value file without overriding process environment."""
    configured_path = os.environ.get("ADAPTIVE_SEARCH_SECRETS")
    path = Path(configured_path).expanduser() if configured_path else (
        Path.home() / ".config" / "searchx" / "secrets.env"
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and name not in os.environ:
            os.environ[name] = value


load_secret_env()


def env_value(provider: str) -> str | None:
    for name in PROVIDER_ENV.get(provider, ()):
        value = os.environ.get(name)
        if value:
            return value
    return None


def searxng_url() -> str | None:
    value = os.environ.get("SEARXNG_URL")
    return value.rstrip("/") if value else None


def configured(provider: str) -> bool:
    if provider == "searxng":
        return bool(searxng_url())
    return bool(env_value(provider))


def decode_body(raw: bytes, headers) -> bytes:
    encoding = ""
    try:
        encoding = headers.get("Content-Encoding", "").lower()
    except Exception:
        pass
    if "gzip" in encoding:
        try:
            return gzip.decompress(raw)
        except OSError:
            pass
    return raw


def request_json(
    provider: str,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, object] | None = None,
    payload: dict | None = None,
    timeout: float = 18,
) -> tuple[object, dict[str, str]]:
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Accept-Encoding", "identity")
    if params:
        pairs = []
        for key, value in params.items():
            if value is not None:
                pairs.append((key, value))
        suffix = urlencode(pairs, doseq=True)
        url = url + ("&" if "?" in url else "?") + suffix
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = decode_body(response.read(), response.headers)
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError as exc:
                raise ProviderError(provider, response.status, f"invalid JSON: {exc}") from exc
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return data, response_headers
    except HTTPError as exc:
        raw = decode_body(exc.read(), exc.headers)
        try:
            detail = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            detail = raw.decode("utf-8", "replace")[:500]
        raise ProviderError(provider, exc.code, compact_text(detail)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderError(provider, None, f"{type(exc).__name__}: {exc}") from exc


def compact_text(value: object, limit: int = 800) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    return " ".join(text.split())[:limit]


def clean_text(value: object, limit: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean_text(item, limit) for item in value)
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:limit]


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
               "utm_content", "gclid", "fbclid", "ref", "referrer"}
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in ignored and not key.lower().startswith("utm_")]
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(sorted(query)), ""))


def make_result(
    provider: str,
    rank: int,
    title: object,
    url: object,
    snippet: object,
    *,
    published_at: object = None,
    native_score: object = None,
    source_kind: str | None = None,
) -> dict | None:
    if not isinstance(url, str):
        return None
    normalized = canonical_url(url)
    if not normalized:
        return None
    score = None
    if isinstance(native_score, (int, float)):
        score = float(native_score)
    return {
        "provider": provider,
        "title": clean_text(title, 300),
        "url": normalized,
        "snippet": clean_text(snippet, 1400),
        "published_at": clean_text(published_at, 80) or None,
        "domain": domain_of(normalized),
        "provider_rank": rank,
        "native_score": score,
        "source_kind": source_kind or provider,
    }


def normalize_brave(data: object, provider: str = "brave") -> list[dict]:
    output = []
    if not isinstance(data, dict):
        return output
    for kind in ("web", "news"):
        results = data.get(kind, {}).get("results", []) if isinstance(data.get(kind), dict) else []
        for rank, item in enumerate(results, 1):
            if not isinstance(item, dict):
                continue
            row = make_result(
                provider, rank, item.get("title"), item.get("url"),
                item.get("description") or item.get("snippet"),
                published_at=item.get("page_age") or item.get("age"),
                source_kind=kind,
            )
            if row:
                output.append(row)
    return output


def normalize_exa(data: object) -> list[dict]:
    output = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for rank, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        highlights = item.get("highlights") or item.get("summary") or item.get("text")
        row = make_result(
            "exa", rank, item.get("title"), item.get("url"), highlights,
            published_at=item.get("publishedDate") or item.get("published_date"),
        )
        if row:
            output.append(row)
    return output


def normalize_tavily(data: object) -> list[dict]:
    output = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for rank, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        row = make_result(
            "tavily", rank, item.get("title"), item.get("url"),
            item.get("content") or item.get("raw_content"),
            published_at=item.get("published_date"),
            native_score=item.get("score"),
        )
        if row:
            output.append(row)
    return output


def normalize_baidu(data: object) -> list[dict]:
    output = []
    results = data.get("references", []) if isinstance(data, dict) else []
    for rank, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        row = make_result(
            "baidu", rank, item.get("title"), item.get("url"),
            item.get("markdown_content") or item.get("snippet") or item.get("content"),
            published_at=item.get("date"),
            native_score=item.get("authority_score"),
            source_kind="cn_web",
        )
        if row:
            output.append(row)
    return output


def normalize_newsapi(data: object) -> list[dict]:
    output = []
    articles = data.get("articles", []) if isinstance(data, dict) else []
    for rank, item in enumerate(articles, 1):
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        row = make_result(
            "newsapi", rank, item.get("title"), item.get("url"),
            item.get("description") or item.get("content"),
            published_at=item.get("publishedAt"),
            source_kind=f"news:{source.get('name') or 'article'}",
        )
        if row:
            output.append(row)
    return output


def normalize_github(data: object, search_type: str) -> list[dict]:
    output = []
    items = data.get("items", []) if isinstance(data, dict) else []
    for rank, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        title = item.get("full_name") or item.get("name") or item.get("path")
        snippet = item.get("description") or item.get("text_matches") or item.get("body")
        published = item.get("updated_at") or item.get("pushed_at")
        row = make_result(
            "github", rank, title, item.get("html_url"),
            snippet, published_at=published, source_kind=f"github:{search_type}",
        )
        if row:
            output.append(row)
    return output


def normalize_serper(data: object) -> list[dict]:
    output = []
    if not isinstance(data, dict):
        return output
    rows = data.get("organic", []) or data.get("news", [])
    for rank, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            continue
        row = make_result(
            "serper", rank, item.get("title"), item.get("link") or item.get("url"),
            item.get("snippet") or item.get("description"),
            published_at=item.get("date"),
            source_kind="google_serp",
        )
        if row:
            output.append(row)
    return output


def normalize_searxng(data: object) -> list[dict]:
    output = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for rank, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        row = make_result(
            "searxng", rank, item.get("title"), item.get("url"),
            item.get("content") or item.get("snippet"),
            published_at=item.get("publishedDate"),
            source_kind="metasearch",
        )
        if row:
            output.append(row)
    return output


def freshness_value(value: str) -> str | None:
    return {
        "day": "pd",
        "week": "pw",
        "month": "pm",
        "year": "py",
    }.get(value)


def date_from_freshness(value: str) -> str | None:
    days = {"day": 1, "week": 7, "month": 31, "year": 365}.get(value)
    if not days:
        return None
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()


def search_brave(query: str, ctx: dict) -> tuple[list[dict], dict]:
    params = {
        "q": query,
        "count": ctx["limit"],
        "result_filter": "news" if ctx["is_news"] else "web",
    }
    if ctx["locale"] == "cn":
        params.update({"search_lang": "zh-hans", "country": "CN"})
    else:
        params["search_lang"] = "en"
    if ctx["freshness"]:
        params["freshness"] = freshness_value(ctx["freshness"])
    data, headers = request_json(
        "brave",
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": env_value("brave") or ""},
        params=params,
    )
    return normalize_brave(data), {
        "rate_remaining": headers.get("x-ratelimit-remaining"),
        "rate_reset": headers.get("x-ratelimit-reset"),
    }


def search_exa(query: str, ctx: dict) -> tuple[list[dict], dict]:
    search_type = {"quick": "instant", "balanced": "auto", "deep": "deep-lite"}[ctx["depth"]]
    payload = {
        "query": query,
        "type": search_type,
        "numResults": ctx["limit"],
        "contents": {"highlights": True},
    }
    if ctx["is_news"]:
        payload["category"] = "news"
    data, _ = request_json(
        "exa",
        "https://api.exa.ai/search",
        headers={"x-api-key": env_value("exa") or ""},
        method="POST",
        payload=payload,
    )
    return normalize_exa(data), {"search_type": search_type}


def search_tavily(query: str, ctx: dict) -> tuple[list[dict], dict]:
    search_depth = {"quick": "fast", "balanced": "basic", "deep": "advanced"}[ctx["depth"]]
    payload = {
        "query": query,
        "search_depth": search_depth,
        "max_results": ctx["limit"],
        "topic": "news" if ctx["is_news"] else "general",
        "include_answer": False,
        "include_raw_content": False,
        "include_usage": True,
    }
    data, _ = request_json(
        "tavily",
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {env_value('tavily') or ''}"},
        method="POST",
        payload=payload,
    )
    usage = data.get("usage") if isinstance(data, dict) else None
    return normalize_tavily(data), {"usage": usage}


def search_baidu(query: str, ctx: dict) -> tuple[list[dict], dict]:
    payload = {
        "messages": [{"content": query, "role": "user"}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": ctx["limit"]}],
    }
    data, _ = request_json(
        "baidu",
        "https://qianfan.baidubce.com/v2/ai_search/web_search",
        headers={"X-Appbuilder-Authorization": f"Bearer {env_value('baidu') or ''}"},
        method="POST",
        payload=payload,
    )
    return normalize_baidu(data), {}


def search_newsapi(query: str, ctx: dict) -> tuple[list[dict], dict]:
    params = {
        "q": query,
        "pageSize": ctx["limit"],
        "sortBy": "publishedAt",
        "language": "zh" if ctx["locale"] == "cn" else "en",
    }
    start = date_from_freshness(ctx["freshness"] or ("week" if ctx["is_news"] else ""))
    if start:
        params["from"] = start
    data, _ = request_json(
        "newsapi",
        "https://newsapi.org/v2/everything",
        headers={"X-Api-Key": env_value("newsapi") or ""},
        params=params,
    )
    return normalize_newsapi(data), {}


def search_github(query: str, ctx: dict) -> tuple[list[dict], dict]:
    search_type = ctx["github_type"]
    endpoint = {
        "repositories": "repositories",
        "code": "code",
        "issues": "issues",
        "users": "users",
    }.get(search_type, "repositories")
    params = {"q": query, "per_page": ctx["limit"]}
    if search_type in ("repositories", "issues"):
        params["sort"] = "updated"
        params["order"] = "desc"
    data, headers = request_json(
        "github",
        f"https://api.github.com/search/{endpoint}",
        headers={
            "Authorization": f"Bearer {env_value('github') or ''}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "adaptive-search",
        },
        params=params,
    )
    return normalize_github(data, search_type), {
        "rate_remaining": headers.get("x-ratelimit-remaining"),
        "rate_reset": headers.get("x-ratelimit-reset"),
    }


def search_serper(query: str, ctx: dict) -> tuple[list[dict], dict]:
    payload = {
        "q": query,
        "num": ctx["limit"],
        "hl": "zh-cn" if ctx["locale"] == "cn" else "en",
    }
    data, _ = request_json(
        "serper",
        "https://google.serper.dev/search",
        headers={"X-API-KEY": env_value("serper") or ""},
        method="POST",
        payload=payload,
    )
    return normalize_serper(data), {}


def search_searxng(query: str, ctx: dict) -> tuple[list[dict], dict]:
    data, _ = request_json(
        "searxng",
        searxng_url() or "",
        params={
            "q": query,
            "format": "json",
            "language": "zh-CN" if ctx["locale"] == "cn" else "en",
        },
        timeout=10,
    )
    return normalize_searxng(data), {}


SEARCHERS = {
    "brave": search_brave,
    "exa": search_exa,
    "tavily": search_tavily,
    "baidu": search_baidu,
    "newsapi": search_newsapi,
    "github": search_github,
    "serper": search_serper,
    "searxng": search_searxng,
}


def cache_path() -> Path:
    configured_path = os.environ.get("ADAPTIVE_SEARCH_CACHE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path.home() / ".cache" / "adaptive-search" / "cache.sqlite3"


def open_store() -> sqlite3.Connection:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS cache "
        "(cache_key TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS meta "
        "(meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS task_search_usage "
        "(usage_id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL, "
        "request_key TEXT NOT NULL, created REAL NOT NULL, "
        "provider TEXT NOT NULL, query TEXT NOT NULL)"
    )
    connection.commit()
    return connection


def cache_ttl(intent: str, is_news: bool) -> int:
    if is_news or intent == "news":
        return 300
    if intent == "cn":
        return 2 * 60 * 60
    return 6 * 60 * 60


def cache_key(provider: str, query: str, ctx: dict) -> str:
    selected = {
        "provider": provider,
        "query": query,
        "intent": ctx["intent"],
        "depth": ctx["depth"],
        "locale": ctx["locale"],
        "freshness": ctx["freshness"],
        "github_type": ctx["github_type"],
        "is_comparison": ctx["is_comparison"],
    }
    return hashlib.sha256(
        json.dumps(selected, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def cache_get(store: sqlite3.Connection, key: str, ttl: int) -> list[dict] | None:
    row = store.execute(
        "SELECT created, payload FROM cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    if time.time() - float(row[0]) > ttl:
        store.execute("DELETE FROM cache WHERE cache_key = ?", (key,))
        store.commit()
        return None
    try:
        return json.loads(row[1])
    except json.JSONDecodeError:
        return None


def cache_set(store: sqlite3.Connection, key: str, payload: list[dict]) -> None:
    store.execute(
        "INSERT OR REPLACE INTO cache(cache_key, created, payload) VALUES (?, ?, ?)",
        (key, time.time(), json.dumps(payload, ensure_ascii=False)),
    )
    store.commit()


def task_scope_for(explicit_scope: str | None = None) -> tuple[str | None, str]:
    if explicit_scope and explicit_scope.strip():
        return explicit_scope.strip(), "explicit"
    for name in ("ADAPTIVE_SEARCH_TASK_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip(), name
    return None, "legacy_query"


def task_key_for(task_query: str, scope: str | None = None) -> str:
    material = "\n".join((TASK_KEY_VERSION, scope or "legacy", task_query.strip().lower()))
    return hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()


def task_usage_count(store: sqlite3.Connection, task_key: str) -> int:
    cutoff = time.time() - TASK_BUDGET_TTL
    store.execute(
        "DELETE FROM task_search_usage WHERE created < ?", (cutoff,)
    )
    count = store.execute(
        "SELECT COUNT(*) FROM task_search_usage "
        "WHERE task_key = ? AND created >= ?",
        (task_key, cutoff),
    ).fetchone()[0]
    store.commit()
    return int(count)


def task_budget_allows(store: sqlite3.Connection, task_key: str, budget: int) -> bool:
    """Compatibility helper; depth limits, not the ledger, control execution."""
    return True


def record_task_request(store: sqlite3.Connection, task_key: str,
                        request_key: str, provider: str, query: str) -> None:
    store.execute(
        "INSERT INTO task_search_usage "
        "(task_key, request_key, created, provider, query) VALUES (?, ?, ?, ?, ?)",
        (task_key, request_key, time.time(), provider, query),
    )
    store.commit()


def task_used_providers(store: sqlite3.Connection, task_key: str) -> set[str]:
    task_usage_count(store, task_key)
    rows = store.execute(
        "SELECT DISTINCT provider FROM task_search_usage WHERE task_key = ?",
        (task_key,),
    ).fetchall()
    return {row[0] for row in rows}


def round_robin_provider(store: sqlite3.Connection, available: set[str]) -> str | None:
    candidates = ["brave", "brave", "exa", "tavily"]
    candidates = [item for item in candidates if item in available]
    if not candidates:
        return None
    key = "rotation:web"
    row = store.execute("SELECT meta_value FROM meta WHERE meta_key = ?", (key,)).fetchone()
    index = int(row[0]) if row else 0
    provider = candidates[index % len(candidates)]
    store.execute(
        "INSERT OR REPLACE INTO meta(meta_key, meta_value) VALUES (?, ?)",
        (key, str(index + 1)),
    )
    store.commit()
    return provider


def detect_intent(query: str, requested: str, locale: str) -> str:
    if requested != "auto":
        return requested
    lowered = query.lower()
    if any(word in lowered for word in CODE_WORDS):
        return "code"
    if locale == "cn" or any(word in query for word in CN_WORDS):
        return "cn"
    if any(word in lowered or word in query for word in NEWS_WORDS):
        return "news"
    if any(word in lowered or word in query for word in RESEARCH_WORDS):
        return "research"
    if any(word in lowered or word in query for word in SEMANTIC_WORDS):
        return "semantic"
    if any(char >= "\u4e00" and char <= "\u9fff" for char in query):
        if not any(word in lowered for word in GLOBAL_WORDS):
            return "cn"
    return "web"


def detect_depth(query: str, requested: str, intent: str, is_news: bool) -> str:
    if requested != "auto":
        return requested
    lowered = query.lower()
    if any(word in lowered or word in query for word in RESEARCH_WORDS):
        return "deep"
    if any(word in lowered or word in query for word in COMPARISON_WORDS):
        return "balanced"
    if intent in ("research", "news", "cn") or any(
        word in lowered or word in query for word in SEMANTIC_WORDS
    ):
        return "balanced"
    return "quick"


def choose_chain(
    store: sqlite3.Connection,
    intent: str,
    is_news: bool,
    available: set[str],
    *,
    explicit_provider: str | None = None,
    allow_reserve: bool = False,
) -> list[str]:
    if explicit_provider:
        return [explicit_provider]
    if intent == "google":
        chain = ["serper", "brave", "exa"]
    elif intent == "code":
        chain = ["github", "brave", "exa"]
    elif intent == "cn":
        chain = ["baidu", "newsapi", "tavily", "brave"] if is_news else [
            "baidu", "brave", "exa", "tavily"
        ]
    elif intent == "news":
        chain = ["newsapi", "tavily", "brave", "exa"]
    elif intent in ("semantic", "research"):
        chain = ["exa", "tavily", "brave"]
    else:
        primary = round_robin_provider(store, available)
        chain = [primary] if primary else []
        chain.extend(["brave", "exa", "tavily"])
    output = []
    for provider in chain:
        if provider in available and provider not in output:
            output.append(provider)
    if allow_reserve and "serper" in available and "serper" not in output:
        output.append("serper")
    if "searxng" in available and "searxng" not in output:
        output.append("searxng")
    return output


def unique_domains(results: list[dict]) -> int:
    return len({item.get("domain") for item in results if item.get("domain")})


def is_comparison_query(query: str) -> bool:
    lowered = query.lower()
    return any(word in lowered or word in query for word in COMPARISON_WORDS)


def quality_snapshot(
    results: list[dict],
    *,
    depth: str,
    intent: str,
    is_news: bool,
    is_comparison: bool,
    provider_count: int,
    single_provider_requested: bool,
) -> dict:
    usable = [
        item for item in results
        if item.get("url") and item.get("title")
    ]
    domains = unique_domains(usable)
    minimum_results = 3 if depth == "quick" else (5 if depth == "deep" else 4)
    minimum_domains = 1 if intent == "code" else (2 if depth == "quick" else 3)
    reasons: list[str] = []

    if not usable:
        reasons.append("no_results")
    elif len(usable) < minimum_results:
        reasons.append("sparse_results")
    if domains < minimum_domains:
        reasons.append("low_domain_diversity")

    if usable:
        snippet_coverage = sum(bool(item.get("snippet")) for item in usable) / len(usable)
        if snippet_coverage < 0.6:
            reasons.append("weak_snippets")
    else:
        snippet_coverage = 0.0

    if is_news and usable and not any(item.get("published_at") for item in usable):
        reasons.append("missing_freshness")

    # Deep means multi-source by default. Balanced only requires an
    # independent provider for research, comparisons, and current claims.
    # An explicit --provider request is a deliberate single-source override.
    needs_independent = not single_provider_requested and (
        depth == "deep"
        or (
            depth == "balanced"
            and (intent in ("research", "news") or is_news or is_comparison)
        )
    )
    if needs_independent and provider_count < 2:
        reasons.append("independent_search_needed")

    return {
        "usable_results": len(usable),
        "unique_domains": domains,
        "snippet_coverage": round(snippet_coverage, 2),
        "providers": provider_count,
        "independent_required": needs_independent,
        "sufficient": not reasons,
        "needs_followup": bool(reasons),
        "reasons": reasons,
    }


def merge_results(grouped: list[tuple[str, list[dict]]], limit: int) -> list[dict]:
    by_url: dict[str, dict] = {}
    provider_position = {provider: index for index, (provider, _) in enumerate(grouped)}
    for provider, rows in grouped:
        for row in rows:
            key = row["url"]
            if key in by_url:
                existing = by_url[key]
                providers = existing.setdefault("providers", [existing["provider"]])
                if provider not in providers:
                    providers.append(provider)
                if len(row.get("snippet", "")) > len(existing.get("snippet", "")):
                    existing["snippet"] = row["snippet"]
                continue
            item = dict(row)
            item["providers"] = [provider]
            item["_fusion_score"] = (
                1000
                - provider_position.get(provider, 99) * 100
                - row.get("provider_rank", 99)
                + min(row.get("native_score") or 0, 1) * 5
            )
            by_url[key] = item
    ordered = sorted(by_url.values(), key=lambda item: item["_fusion_score"], reverse=True)
    selected = []
    domain_counts: dict[str, int] = {}
    for item in ordered:
        domain = item.get("domain") or ""
        if domain_counts.get(domain, 0) >= 2 and len(ordered) - len(selected) > limit:
            continue
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        item.pop("_fusion_score", None)
        item["rank"] = len(selected) + 1
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def provider_status() -> dict:
    status = {}
    for provider in ("brave", "exa", "tavily", "baidu", "newsapi", "github",
                     "firecrawl", "serper"):
        names = PROVIDER_ENV[provider]
        status[provider] = {
            "configured": configured(provider),
            "environment": next((name for name in names if os.environ.get(name)), None),
        }
    status["searxng"] = {
        "configured": bool(searxng_url()),
        "endpoint": searxng_url(),
    }
    return status


def build_context(args: argparse.Namespace) -> dict:
    intent = detect_intent(args.query, args.intent, args.locale)
    is_comparison = is_comparison_query(args.query)
    is_news = intent == "news" or (
        intent == "cn"
        and any(word in args.query.lower() or word in args.query for word in NEWS_WORDS)
    )
    depth = detect_depth(args.query, args.depth, intent, is_news)
    locale = args.locale
    if locale == "auto":
        locale = "cn" if intent == "cn" else "global"
    freshness = args.freshness
    if freshness == "auto":
        freshness = "week" if is_news else None
    return {
        "intent": intent,
        "depth": depth,
        "locale": locale,
        "freshness": freshness,
        "is_news": is_news,
        "is_comparison": is_comparison,
        "limit": RESULT_LIMITS[depth],
        "github_type": args.github_type,
    }


def run_search(args: argparse.Namespace) -> dict:
    store = open_store()
    ctx = build_context(args)
    task_query = args.task_query or os.environ.get("ADAPTIVE_SEARCH_TASK_QUERY") or args.query
    task_scope, task_scope_source = task_scope_for(args.task_id)
    task_key = task_key_for(task_query, task_scope)
    task_budget = TASK_PROVIDER_BUDGETS[ctx["depth"]]
    used_task_providers = task_used_providers(store, task_key)
    available = {provider for provider in SEARCHERS if configured(provider)}
    if args.provider:
        available.add(args.provider)
    chain = choose_chain(
        store,
        ctx["intent"],
        ctx["is_news"],
        available,
        explicit_provider=args.provider,
        allow_reserve=args.allow_reserve,
    )
    if not args.provider:
        # A different provider is a more useful follow-up than another query
        # against the same index. Keep the intent-specific order among fresh
        # providers, and only reuse a provider as a fallback when necessary.
        fresh_chain = [provider for provider in chain if provider not in used_task_providers]
        used_chain = [provider for provider in chain if provider in used_task_providers]
        chain = fresh_chain + used_chain
    max_rounds = DEPTH_LIMITS[ctx["depth"]]
    grouped: list[tuple[str, list[dict]]] = []
    rounds = []
    errors = []
    cached_count = 0

    for round_number, provider in enumerate(chain[:max_rounds], 1):
        if provider not in SEARCHERS:
            continue
        if not configured(provider) and provider != args.provider:
            rounds.append({"round": round_number, "provider": provider, "status": "skipped_missing_key"})
            continue
        key = cache_key(provider, args.query, ctx)
        cached = cache_get(store, key, cache_ttl(ctx["intent"], ctx["is_news"]))
        if cached is not None:
            rows = cached
            cached_count += 1
            rounds.append({
                "round": round_number,
                "provider": provider,
                "status": "cached",
                "result_count": len(rows),
            })
        else:
            # The ledger is telemetry and provider-rotation state only. The
            # per-invocation depth limit above is the execution guardrail;
            # stale activity from another session must never block round one.
            record_task_request(store, task_key, key, provider, args.query)
            try:
                rows, metadata = SEARCHERS[provider](args.query, ctx)
                cache_set(store, key, rows)
                rounds.append({
                    "round": round_number,
                    "provider": provider,
                    "status": "ok",
                    "result_count": len(rows),
                    "metadata": metadata,
                })
            except ProviderError as exc:
                errors.append({
                    "provider": exc.provider,
                    "status": exc.status,
                    "message": exc.message,
                })
                rounds.append({
                    "round": round_number,
                    "provider": provider,
                    "status": "error",
                    "http_status": exc.status,
                    "message": exc.message,
                })
                continue
        grouped.append((provider, rows))
        merged = merge_results(grouped, RESULT_LIMITS[ctx["depth"]])
        quality = quality_snapshot(
            merged,
            depth=ctx["depth"],
            intent=ctx["intent"],
            is_news=ctx["is_news"],
            is_comparison=ctx["is_comparison"],
            provider_count=len(grouped),
            single_provider_requested=bool(args.provider),
        )
        rounds[-1]["quality_after_round"] = quality
        if round_number < max_rounds and (
            quality["needs_followup"]
            or (args.force_followup and round_number == 1)
        ):
            continue
        break

    sources = merge_results(grouped, RESULT_LIMITS[ctx["depth"]])
    quality = quality_snapshot(
        sources,
        depth=ctx["depth"],
        intent=ctx["intent"],
        is_news=ctx["is_news"],
        is_comparison=ctx["is_comparison"],
        provider_count=len(grouped),
        single_provider_requested=bool(args.provider),
    )
    attempted = {round_info["provider"] for round_info in rounds}
    next_provider = next((provider for provider in chain if provider not in attempted), None)
    remaining_rounds = max(0, max_rounds - len(rounds))
    task_used = task_usage_count(store, task_key)
    task_remaining = max(0, task_budget - task_used)
    task_providers_used = sorted(task_used_providers(store, task_key))
    return {
        "ok": bool(sources),
        "query": args.query,
        "intent": ctx["intent"],
        "depth": ctx["depth"],
        "locale": ctx["locale"],
        "freshness": ctx["freshness"],
        "providers_available": sorted(available),
        "providers_used": [provider for provider, _ in grouped],
        "cached_provider_results": cached_count,
        "rounds": rounds,
        "errors": errors,
        "task": {
            "budget": task_budget,
            "used": task_used,
            "remaining": task_remaining,
            "providers_used": task_providers_used,
            "budget_exhausted": False,
            "budget_mode": "advisory_only",
            "scope_source": task_scope_source,
            "task_query_explicit": bool(args.task_query or os.environ.get("ADAPTIVE_SEARCH_TASK_QUERY")),
        },
        "quality": quality,
        "follow_up": {
            "recommended": (
                quality["needs_followup"]
                and bool(next_provider)
                and remaining_rounds > 0
            ),
            "next_provider": next_provider,
            "remaining_rounds": remaining_rounds,
            "reasons": quality["reasons"],
            "force_followup": args.force_followup,
        },
        "sources": sources,
    }


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", help="Search query")
    parser.add_argument(
        "--task-query",
        help="Original user question shared by all searches in one task; used for ledger and cache context",
    )
    parser.add_argument(
        "--task-id",
        help="Optional task/session scope; Codex automatically uses CODEX_THREAD_ID when available",
    )
    parser.add_argument("--intent", choices=["auto", "web", "semantic", "research",
                                             "news", "cn", "code", "google"], default="auto")
    parser.add_argument("--depth", choices=["auto", "quick", "balanced", "deep"], default="auto")
    parser.add_argument("--locale", choices=["auto", "global", "cn"], default="auto")
    parser.add_argument("--freshness", choices=["auto", "day", "week", "month", "year"],
                        default="auto")
    parser.add_argument("--github-type", choices=["repositories", "code", "issues", "users"],
                        default="repositories")
    parser.add_argument("--provider", choices=sorted(list(SEARCHERS)), help="Explicit provider")
    parser.add_argument("--allow-reserve", action="store_true",
                        help="Allow Serper as an explicit reserve provider")
    parser.add_argument("--force-followup", action="store_true",
                        help="Run one additional provider even when the first result set is sufficient")
    parser.add_argument("--status", action="store_true", help="Show configured providers")
    parser.add_argument("--json-indent", type=int, default=None)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    if args.status:
        print(json.dumps({
            "skill": "adaptive-search",
            "quota_preflight": False,
            "provider_status": provider_status(),
            "policy": {
                "quick_max_search_providers": 2,
                "balanced_max_search_providers": 2,
                "deep_min_search_providers": 2,
                "deep_max_search_providers": 3,
                "second_search": "quick/balanced conditional; deep required unless --provider is explicit",
                "task_provider_budgets": TASK_PROVIDER_BUDGETS,
                "task_budget_mode": "advisory_only; depth limits cap rounds per invocation",
                "task_budget_ttl_hours": TASK_BUDGET_TTL // 3600,
                "task_budget_key": "--task-id, ADAPTIVE_SEARCH_TASK_ID, CODEX_THREAD_ID, or CODEX_SESSION_ID plus --task-query",
                "task_followup_provider_policy": "prefer_unused_providers",
                "general_rotation": "brave:exa:tavily=2:1:1",
                "serper": "reserve_only",
                "firecrawl": "fetch_only",
            },
        }, ensure_ascii=False, indent=args.json_indent))
        return 0
    if not args.query or not args.query.strip():
        print(json.dumps({"ok": False, "error": "--query is required"}, ensure_ascii=False))
        return 2
    if not (args.task_query or os.environ.get("ADAPTIVE_SEARCH_TASK_QUERY")):
        print(json.dumps({
            "ok": False,
            "error": "--task-query is required for task-level search budgeting",
        }, ensure_ascii=False))
        return 2
    output = run_search(args)
    print(json.dumps(output, ensure_ascii=False, indent=args.json_indent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
