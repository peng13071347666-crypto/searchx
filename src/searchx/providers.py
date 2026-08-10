from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import api_key
from .http import HttpClient, HttpError
from .models import ProviderCall, SearchResult


def _is_zh(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _domain_query(query: str, domains: list[str] | None) -> str:
    if not domains:
        return query
    clean = [d.strip().lower().removeprefix("www.") for d in domains if d.strip()]
    if not clean:
        return query
    if len(clean) == 1:
        return f"{query} site:{clean[0]}"
    return f"{query} (" + " OR ".join(f"site:{d}" for d in clean[:8]) + ")"


def _failure(provider: str, query: str, start: float, exc: Exception) -> ProviderCall:
    status = exc.status if isinstance(exc, HttpError) else None
    return ProviderCall(
        provider=provider,
        query=query,
        status="error",
        error=str(exc),
        http_status=status,
        elapsed_ms=(time.perf_counter() - start) * 1000,
    )


def _nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _serper_author(item: Mapping[str, Any]) -> str | None:
    source = _nonempty_text(item.get("source"))
    if source:
        return source
    publication_info = item.get("publicationInfo")
    if isinstance(publication_info, Mapping):
        return _nonempty_text(publication_info.get("summary"))
    return _nonempty_text(publication_info)


class BaseProvider:
    name = "base"
    env_key = ""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(api_key(self.name))

    def require_key(self) -> str:
        key = api_key(self.name)
        if not key:
            raise RuntimeError(f"{self.name} is not configured")
        return key

    def search(self, query: str, **kwargs: Any) -> ProviderCall:
        raise NotImplementedError


class SerperProvider(BaseProvider):
    name = "serper"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "web",
        freshness: str | None = None,
        domains: list[str] | None = None,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            endpoint = "search"
            if mode == "news":
                endpoint = "news"
            elif mode == "academic":
                endpoint = "scholar"
            body: dict[str, Any] = {
                "q": _domain_query(query, domains),
                "num": min(max(limit, 1), 20),
                "hl": "zh-cn" if _is_zh(query) else "en",
                "gl": "cn" if _is_zh(query) else "us",
            }
            if freshness:
                qdr = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(freshness)
                if qdr:
                    body["tbs"] = f"qdr:{qdr}"
            response = self.http.request_json(
                "POST",
                f"https://google.serper.dev/{endpoint}",
                headers={"X-API-KEY": key},
                body=body,
            )
            data = response.data if isinstance(response.data, Mapping) else {}
            rows = data.get("news") if endpoint == "news" else data.get("organic")
            rows = rows if isinstance(rows, list) else []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                if not isinstance(item, Mapping):
                    continue
                url = _nonempty_text(item.get("link")) or _nonempty_text(item.get("url")) or ""
                if not url:
                    continue
                results.append(
                    SearchResult(
                        title=_nonempty_text(item.get("title")) or url,
                        url=url,
                        snippet=_nonempty_text(item.get("snippet")) or _nonempty_text(item.get("description")) or "",
                        provider=self.name,
                        rank=i,
                        published_at=_nonempty_text(item.get("date")) or _nonempty_text(item.get("year")),
                        author=_serper_author(item),
                        metadata={"endpoint": endpoint},
                    )
                )
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                metadata={"endpoint": endpoint},
            )
        except Exception as exc:  # provider boundary
            return _failure(self.name, query, start, exc)


class BraveProvider(BaseProvider):
    name = "brave"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "web",
        freshness: str | None = None,
        domains: list[str] | None = None,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            is_news = mode == "news"
            endpoint = "news/search" if is_news else "web/search"
            params: dict[str, Any] = {
                "q": _domain_query(query, domains),
                "count": min(max(limit, 1), 20),
                "freshness": {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(freshness or ""),
                "search_lang": "zh-hans" if _is_zh(query) else "en",
                "country": "CN" if _is_zh(query) else "US",
                "extra_snippets": "true",
                "text_decorations": "false",
            }
            response = self.http.request_json(
                "GET",
                f"https://api.search.brave.com/res/v1/{endpoint}",
                headers={"X-Subscription-Token": key, "Accept-Encoding": "gzip"},
                params=params,
            )
            if is_news:
                rows = response.data.get("results") or response.data.get("news", {}).get("results") or []
            else:
                rows = response.data.get("web", {}).get("results") or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                url = item.get("url") or ""
                if not url:
                    continue
                extras = item.get("extra_snippets") or []
                snippet = item.get("description") or item.get("snippet") or ""
                if extras:
                    snippet = (snippet + "\n" + "\n".join(extras[:2])).strip()
                results.append(
                    SearchResult(
                        title=item.get("title") or url,
                        url=url,
                        snippet=snippet,
                        provider=self.name,
                        rank=i,
                        published_at=item.get("age") or item.get("page_age") or item.get("date"),
                        metadata={"endpoint": endpoint},
                    )
                )
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                metadata={"endpoint": endpoint},
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)


class TavilyProvider(BaseProvider):
    name = "tavily"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "web",
        freshness: str | None = None,
        domains: list[str] | None = None,
        depth: str | None = None,
        full_content: bool = False,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            search_depth = depth if depth in {"basic", "advanced"} else "basic"
            body: dict[str, Any] = {
                "query": query,
                "search_depth": search_depth,
                "max_results": min(max(limit, 1), 20),
                "topic": "news" if mode == "news" else "general",
                "include_answer": False,
                "include_raw_content": "markdown" if full_content else False,
                "include_domains": domains or [],
                "time_range": freshness if freshness in {"day", "week", "month", "year"} else None,
                "include_usage": True,
                "auto_parameters": False,
            }
            if search_depth == "advanced":
                body["chunks_per_source"] = 3
            response = self.http.request_json(
                "POST",
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {key}"},
                body=body,
            )
            rows = response.data.get("results") or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                url = item.get("url") or ""
                if not url:
                    continue
                results.append(
                    SearchResult(
                        title=item.get("title") or url,
                        url=url,
                        snippet=item.get("content") or "",
                        content=item.get("raw_content"),
                        provider=self.name,
                        rank=i,
                        provider_score=item.get("score"),
                        published_at=item.get("published_date"),
                    )
                )
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                usage=response.data.get("usage") or {},
                metadata={
                    "response_time": response.data.get("response_time"),
                    "search_depth": search_depth,
                    "topic": body["topic"],
                },
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)

    def extract(self, urls: list[str]) -> dict[str, Any]:
        key = self.require_key()
        response = self.http.request_json(
            "POST",
            "https://api.tavily.com/extract",
            headers={"Authorization": f"Bearer {key}"},
            body={"urls": urls, "extract_depth": "advanced", "format": "markdown"},
            timeout=max(self.http.timeout, 25),
        )
        return {"provider": self.name, "elapsed_ms": response.elapsed_ms, **response.data}


class ExaProvider(BaseProvider):
    name = "exa"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "web",
        domains: list[str] | None = None,
        depth: str | None = None,
        category: str | None = None,
        full_content: bool = False,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            search_type = depth if depth in {"instant", "fast", "auto", "deep-lite", "deep", "deep-reasoning"} else "auto"
            category_map = {
                "academic": "research paper",
                "news": "news",
                "company": "company",
                "people": "people",
                "finance": "financial report",
            }
            exa_category = category or category_map.get(mode)
            body: dict[str, Any] = {
                "query": query,
                "type": search_type,
                "numResults": min(max(limit, 1), 20),
                "contents": {
                    "highlights": True,
                    **({"text": {"maxCharacters": 6000}} if full_content else {}),
                },
            }
            if exa_category:
                body["category"] = exa_category
            if domains and exa_category not in {"company", "people"}:
                body["includeDomains"] = domains
            response = self.http.request_json(
                "POST",
                "https://api.exa.ai/search",
                headers={"x-api-key": key},
                body=body,
                timeout=45 if search_type in {"deep", "deep-reasoning"} else None,
            )
            rows = response.data.get("results") or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                url = item.get("url") or item.get("id") or ""
                if not url:
                    continue
                highlights = item.get("highlights") or []
                snippet = "\n".join(highlights) if highlights else item.get("text") or ""
                results.append(
                    SearchResult(
                        title=item.get("title") or url,
                        url=url,
                        snippet=snippet,
                        content=item.get("text") if full_content else None,
                        provider=self.name,
                        rank=i,
                        published_at=item.get("publishedDate"),
                        author=item.get("author"),
                        metadata={"id": item.get("id"), "highlight_scores": item.get("highlightScores") or []},
                    )
                )
            usage: dict[str, Any] = {}
            if response.data.get("costDollars") is not None:
                usage["costDollars"] = response.data.get("costDollars")
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                usage=usage,
                metadata={"search_type": response.data.get("searchType") or search_type, "category": exa_category},
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)

    def contents(self, urls: list[str]) -> dict[str, Any]:
        key = self.require_key()
        response = self.http.request_json(
            "POST",
            "https://api.exa.ai/contents",
            headers={"x-api-key": key},
            body={"urls": urls, "text": {"maxCharacters": 30000}},
            timeout=max(self.http.timeout, 25),
        )
        return {"provider": self.name, "elapsed_ms": response.elapsed_ms, **response.data}


class NewsApiProvider(BaseProvider):
    name = "newsapi"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        freshness: str | None = None,
        domains: list[str] | None = None,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            params: dict[str, Any] = {
                "q": query,
                "pageSize": min(max(limit, 1), 100),
                "language": "zh" if _is_zh(query) else "en",
                "sortBy": "publishedAt" if freshness else "relevancy",
                "domains": ",".join(domains) if domains else None,
            }
            days = {"day": 1, "week": 7, "month": 31, "year": 365}.get(freshness or "")
            if days:
                params["from"] = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            response = self.http.request_json(
                "GET",
                "https://newsapi.org/v2/everything",
                headers={"X-Api-Key": key},
                params=params,
            )
            rows = response.data.get("articles") or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                url = item.get("url") or ""
                if not url:
                    continue
                source = (item.get("source") or {}).get("name")
                results.append(
                    SearchResult(
                        title=item.get("title") or url,
                        url=url,
                        snippet=item.get("description") or item.get("content") or "",
                        provider=self.name,
                        rank=i,
                        published_at=item.get("publishedAt"),
                        author=item.get("author"),
                        metadata={"source": source},
                    )
                )
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                metadata={"total_results": response.data.get("totalResults")},
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)


class GitHubProvider(BaseProvider):
    name = "github"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        category: str | None = None,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            kind = category if category in {"repositories", "code", "issues", "commits"} else "repositories"
            q = query
            if kind == "issues" and "is:issue" not in q:
                q += " is:issue"
            url = f"https://api.github.com/search/{kind}"
            response = self.http.request_json(
                "GET",
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"q": q, "per_page": min(max(limit, 1), 30)},
            )
            rows = response.data.get("items") or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                if kind == "code":
                    repo = item.get("repository") or {}
                    url_value = item.get("html_url") or ""
                    title = f"{repo.get('full_name', '')}:{item.get('path') or item.get('name') or ''}"
                    snippet = f"Code match in {repo.get('full_name', '')} at {item.get('path', '')}"
                else:
                    url_value = item.get("html_url") or ""
                    title = item.get("full_name") or item.get("title") or item.get("sha") or url_value
                    snippet = item.get("description") or item.get("body") or item.get("commit", {}).get("message") or ""
                if not url_value:
                    continue
                results.append(
                    SearchResult(
                        title=title,
                        url=url_value,
                        snippet=snippet[:4000],
                        provider=self.name,
                        rank=i,
                        published_at=item.get("updated_at") or item.get("created_at") or item.get("commit", {}).get("author", {}).get("date"),
                        metadata={
                            "kind": kind,
                            "stars": item.get("stargazers_count"),
                            "language": item.get("language"),
                            "repository": (item.get("repository") or {}).get("full_name"),
                        },
                    )
                )
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                metadata={"kind": kind, "total_count": response.data.get("total_count")},
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)


class FirecrawlProvider(BaseProvider):
    name = "firecrawl"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "web",
        domains: list[str] | None = None,
        full_content: bool = False,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            body: dict[str, Any] = {
                "query": query,
                "limit": min(max(limit, 1), 20),
                "sources": ["news"] if mode == "news" else ["web"],
                "includeDomains": domains or [],
                "highlights": True,
                "ignoreInvalidURLs": True,
            }
            if mode == "code":
                body["categories"] = [{"type": "github"}]
                body["sources"] = ["web"]
            elif mode == "academic":
                body["categories"] = [{"type": "research"}]
            if full_content:
                body["scrapeOptions"] = {"formats": [{"type": "markdown"}]}
            response = self.http.request_json(
                "POST",
                "https://api.firecrawl.dev/v2/search",
                headers={"Authorization": f"Bearer {key}"},
                body=body,
                timeout=max(self.http.timeout, 25 if full_content else self.http.timeout),
            )
            data = response.data.get("data") or {}
            rows = (data.get("news") if mode == "news" else data.get("web")) or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                url = item.get("url") or (item.get("metadata") or {}).get("sourceURL") or ""
                if not url:
                    continue
                results.append(
                    SearchResult(
                        title=item.get("title") or (item.get("metadata") or {}).get("title") or url,
                        url=url,
                        snippet=item.get("description") or item.get("snippet") or (item.get("metadata") or {}).get("description") or "",
                        content=item.get("markdown") if full_content else None,
                        provider=self.name,
                        rank=i,
                        published_at=item.get("date"),
                        metadata={"category": item.get("category")},
                    )
                )
            usage = {}
            if response.data.get("creditsUsed") is not None:
                usage["credits"] = response.data.get("creditsUsed")
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                usage=usage,
                metadata={"warning": response.data.get("warning")},
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)

    def scrape(self, url: str) -> dict[str, Any]:
        key = self.require_key()
        response = self.http.request_json(
            "POST",
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            body={"url": url, "formats": [{"type": "markdown"}]},
            timeout=max(self.http.timeout, 35),
        )
        return {"provider": self.name, "elapsed_ms": response.elapsed_ms, **response.data}


class BaiduProvider(BaseProvider):
    name = "baidu"

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        freshness: str | None = None,
        domains: list[str] | None = None,
        **_: Any,
    ) -> ProviderCall:
        start = time.perf_counter()
        try:
            key = self.require_key()
            body: dict[str, Any] = {
                "messages": [{"content": query, "role": "user"}],
                "search_source": "baidu_search_v2",
                "resource_type_filter": [{"type": "web", "top_k": min(max(limit, 1), 20)}],
            }
            if freshness in {"day", "week", "month", "year"}:
                body["search_recency_filter"] = freshness
            if domains:
                body["search_filter"] = {"match": {"site": domains}}

            try:
                response = self.http.request_json(
                    "POST",
                    "https://qianfan.baidubce.com/v2/ai_search/web_search",
                    headers={"Authorization": f"Bearer {key}"},
                    body=body,
                )
            except HttpError as exc:
                if exc.status not in {401, 403}:
                    raise
                response = self.http.request_json(
                    "POST",
                    "https://qianfan.baidubce.com/v2/ai_search/web_search",
                    headers={"X-Appbuilder-Authorization": f"Bearer {key}"},
                    body=body,
                )
            rows = response.data.get("references") or []
            results: list[SearchResult] = []
            for i, item in enumerate(rows[:limit], 1):
                url = item.get("url") or ""
                if not url:
                    continue
                snippet = item.get("content") or item.get("summary") or item.get("snippet") or item.get("description") or ""
                results.append(
                    SearchResult(
                        title=item.get("title") or url,
                        url=url,
                        snippet=snippet,
                        provider=self.name,
                        rank=i,
                        published_at=item.get("date") or item.get("publish_time") or item.get("published_time"),
                        metadata={"reference_id": item.get("id")},
                    )
                )
            return ProviderCall(
                provider=self.name,
                query=query,
                results=results,
                elapsed_ms=response.elapsed_ms,
                metadata={"request_id": response.data.get("request_id")},
            )
        except Exception as exc:
            return _failure(self.name, query, start, exc)


PROVIDER_CLASSES = {
    "serper": SerperProvider,
    "brave": BraveProvider,
    "tavily": TavilyProvider,
    "exa": ExaProvider,
    "newsapi": NewsApiProvider,
    "github": GitHubProvider,
    "firecrawl": FirecrawlProvider,
    "baidu": BaiduProvider,
}


def build_providers(http: HttpClient) -> dict[str, BaseProvider]:
    return {name: cls(http) for name, cls in PROVIDER_CLASSES.items()}
