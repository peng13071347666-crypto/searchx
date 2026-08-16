#!/usr/bin/env python3
"""Fetch and compact selected page content without using MCP."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from html.parser import HTMLParser
from urllib.request import Request, urlopen

from search import ProviderError, canonical_url, env_value, open_store, request_json

try:
    import trafilatura
except ImportError:  # Keep the local fallback usable before optional setup.
    trafilatura = None


FETCH_CACHE_TTL = 6 * 60 * 60
FETCH_CACHE_VERSION = "trafilatura-v2-safe-compaction"
DEFAULT_MAX_PAGES = 4
# Kept as a CLI compatibility name; it is only a per-invocation page cap.
DEFAULT_FETCH_BUDGET = DEFAULT_MAX_PAGES
DEFAULT_MAX_CHARS = 8000
DEFAULT_CONTEXT_BUDGET = 24000
SOURCE_CHAR_LIMIT = 32000
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024

BLOCK_TAGS = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
    "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "td", "th", "tr", "ul",
}
CJK_RE = re.compile(r"[\u3400-\u9fff]+")
ASCII_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_+#.-]{1,}", re.IGNORECASE)
NOISE_TAGS = {"script", "style", "noscript", "svg", "canvas", "nav", "header", "footer", "aside", "form"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
NOISE_MARKER_RE = re.compile(
    r"(?:^|[-_ ])(?:nav|menu|sidebar|breadcrumb|pagination|cookie|consent|advert|ads|social|share|subscribe|comments?)(?:$|[-_ ])",
    re.IGNORECASE,
)
DYNAMIC_PLACEHOLDER_RE = re.compile(
    r"(?:加载中|正在加载|页面加载中|数据加载中|获取数据中|渲染中|"
    r"loading(?:\s+(?:data|content))?|please\s+wait|initializing|rendering)",
    re.IGNORECASE,
)
OVERVIEW_FALLBACK_MAX_CHARS = 2400


class TextExtractor(HTMLParser):
    def __init__(self, prefer_main: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.prefer_main = prefer_main
        self.content_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr_text = " ".join(
            str(value) for name, value in attrs
            if name.lower() in {"id", "class", "role"} and value
        )
        if self.prefer_main and self.content_depth == 0:
            if tag in {"main", "article"}:
                self.content_depth = 1
            else:
                return
        elif self.prefer_main and tag in {"main", "article"}:
            self.content_depth += 1
        if self.skip_depth > 0 and tag not in VOID_TAGS:
            self.skip_depth += 1
        elif tag in NOISE_TAGS or NOISE_MARKER_RE.search(attr_text):
            self.skip_depth += 1
        elif self.skip_depth == 0 and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth > 0:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.prefer_main and self.content_depth == 0:
            return
        if self.prefer_main and tag.lower() in {"main", "article"}:
            self.content_depth = max(0, self.content_depth - 1)
            if self.content_depth == 0:
                return
        if tag.lower() in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0 and (not self.prefer_main or self.content_depth > 0):
            text = " ".join(data.split())
            if text:
                self.parts.append(text)


def direct_fetch(url: str, max_chars: int) -> tuple[str, dict]:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
            "User-Agent": "adaptive-search/1.0",
        },
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read(MAX_DOWNLOAD_BYTES + 1)
        content_type = response.headers.get_content_charset() or "utf-8"
        media_type = response.headers.get_content_type()
    download_truncated = len(raw) > MAX_DOWNLOAD_BYTES
    if download_truncated:
        raw = raw[:MAX_DOWNLOAD_BYTES]
    text = raw.decode(content_type, "replace")

    # Trafilatura receives the already-downloaded HTML.  Do not use its
    # fetch_url helper here: that would create a second network path and make
    # caching/budget accounting less predictable.
    is_html = media_type in {"text/html", "application/xhtml+xml"} or bool(
        re.search(r"<\s*(?:html|body|main|article|p|div|title)\b", text, re.IGNORECASE)
    )
    if trafilatura is not None and is_html:
        try:
            extracted = trafilatura.extract(
                text,
                url=url,
                output_format="markdown",
                include_comments=False,
                include_tables=True,
                include_links=False,
                favor_precision=True,
                deduplicate=True,
            )
        except Exception as exc:
            extracted = None
            extraction_error = f"{type(exc).__name__}: {exc}"
        else:
            extraction_error = None
        if extracted and extracted.strip():
            return extracted.strip()[:max_chars], {
                "extractor": "trafilatura",
                "trafilatura_version": getattr(trafilatura, "__version__", "unknown"),
                "media_type": media_type,
                "download_bytes": len(raw),
                "download_truncated": download_truncated,
            }
    else:
        extraction_error = None

    # Keep a dependency-free fallback for environments where Trafilatura is
    # unavailable, unsupported by a page, or unable to identify its body.
    if not is_html:
        return text.strip()[:max_chars], {
            "extractor": "plain_text",
            "media_type": media_type,
            "download_bytes": len(raw),
            "download_truncated": download_truncated,
        }

    parser = TextExtractor(prefer_main=bool(re.search(r"<\s*(?:main|article)\b", text, re.IGNORECASE)))
    parser.feed(text)
    metadata = {
        "extractor": "heuristic",
        "media_type": media_type,
        "download_bytes": len(raw),
        "download_truncated": download_truncated,
    }
    if extraction_error:
        metadata["trafilatura_error"] = extraction_error
    return "\n".join(parser.parts)[:max_chars], metadata


def source_char_limit(output_chars: int) -> int:
    """Read enough source text to find relevant blocks, but stay bounded."""
    return min(SOURCE_CHAR_LIMIT, max(output_chars, output_chars * 4))


def query_terms(query: str | None) -> list[tuple[str, float]]:
    if not query:
        return []
    lowered = query.casefold()
    terms: dict[str, float] = {}
    for term in ASCII_TERM_RE.findall(lowered):
        terms[term] = max(terms.get(term, 0.0), 3.0)
    for group in CJK_RE.findall(lowered):
        if len(group) >= 2:
            terms[group] = max(terms.get(group, 0.0), 4.0)
        # Short character n-grams help Chinese queries match pages that use
        # slightly different word boundaries without a tokenizer dependency.
        for size in (3, 4):
            for index in range(0, max(0, len(group) - size + 1)):
                gram = group[index:index + size]
                terms[gram] = max(terms.get(gram, 0.0), 1.0)
    return sorted(terms.items(), key=lambda item: (-item[1], -len(item[0])))


def dynamic_placeholder_reason(content: str) -> str | None:
    """Reject a JS shell that extracted only a loading placeholder."""
    normalized = re.sub(r"\s+", " ", content).strip()
    if not normalized or len(normalized) > 160:
        return None
    if not DYNAMIC_PLACEHOLDER_RE.search(normalized):
        return None
    remainder = DYNAMIC_PLACEHOLDER_RE.sub("", normalized)
    remainder = re.sub(r"[#>*`_\-:：|/\.。…!！~～\s]+", "", remainder)
    if len(remainder) <= 40:
        return "dynamic page returned only a loading placeholder"
    return None


def content_blocks(content: str) -> list[str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            block = " ".join(current).strip()
            if block:
                blocks.append(block)
            current.clear()

    for raw_line in normalized.split("\n"):
        line = " ".join(raw_line.split()).strip()
        if not line:
            flush()
            continue
        if line.startswith("#") and current:
            flush()
        current.append(line)
        if len(" ".join(current)) >= 900:
            flush()
    flush()
    return blocks


def spread_indices(indices: list[int], limit: int) -> list[int]:
    if limit <= 0 or not indices:
        return []
    if len(indices) <= limit:
        return indices
    if limit == 1:
        return [indices[0]]
    selected: list[int] = []
    for position in range(limit):
        source_position = round(position * (len(indices) - 1) / (limit - 1))
        index = indices[source_position]
        if index not in selected:
            selected.append(index)
    return selected


def block_score(block: str, terms: list[tuple[str, float]], index: int) -> float:
    lowered = block.casefold()
    score = 0.0
    for term, weight in terms:
        occurrences = lowered.count(term)
        if occurrences:
            score += weight * min(occurrences, 3)
    if block.startswith("#"):
        score += 1.0
    if index == 0:
        score += 1.0
    return score


def block_has_term(block: str, terms: list[tuple[str, float]]) -> bool:
    lowered = block.casefold()
    return any(term in lowered for term, _ in terms)


def overview_fallback(blocks: list[str], max_chars: int,
                      *, source_chars: int, query_terms_count: int) -> tuple[str, dict]:
    """Return a bounded document overview when lexical relevance is unavailable."""
    fallback_limit = min(
        max_chars,
        max(1000, min(OVERVIEW_FALLBACK_MAX_CHARS, max_chars // 3)),
    )
    selected: dict[int, str] = {}
    used_chars = 0

    def add(index: int) -> None:
        nonlocal used_chars
        if index in selected or used_chars >= fallback_limit:
            return
        block = blocks[index].strip()
        available = fallback_limit - used_chars - 2
        if available <= 0:
            return
        selected_block = block if len(block) <= available else block[:available].rstrip()
        if not selected_block:
            return
        selected[index] = selected_block
        used_chars += len(selected_block) + 2

    # Keep the page identity and lead, then add a few headings and evenly
    # spaced body samples. This gives an overview without returning the body.
    for index in range(min(2, len(blocks))):
        add(index)
    headings = [index for index, block in enumerate(blocks)
                if block.lstrip().startswith("#")]
    for index in spread_indices(headings, 5):
        add(index)
    remaining = [index for index in range(len(blocks)) if index not in selected]
    for index in spread_indices(remaining, 5):
        add(index)

    if not selected:
        add(0)
    compacted = "\n\n".join(selected[index] for index in sorted(selected)).strip()
    return compacted, {
        "mode": "overview_fallback",
        "fallback_reason": "no_lexical_query_match",
        "fallback_limit": fallback_limit,
        "source_chars": source_chars,
        "selected_chars": len(compacted),
        "selected_blocks": len(selected),
        "matched_blocks": 0,
        "query_terms": query_terms_count,
    }


def compact_content(content: str, query: str | None, max_chars: int) -> tuple[str, dict]:
    """Keep the lead and query-relevant blocks within a hard context budget."""
    raw = content.strip()
    blocks = content_blocks(raw)
    if not blocks:
        return "", {
            "mode": "empty",
            "source_chars": len(raw),
            "selected_chars": 0,
            "selected_blocks": 0,
            "matched_blocks": 0,
        }

    terms = query_terms(query)
    scored = [(block_score(block, terms, index), index, block)
              for index, block in enumerate(blocks)]
    matched = sum(1 for _, _, block in scored if block_has_term(block, terms))
    if not terms or matched == 0:
        return overview_fallback(
            blocks,
            max_chars,
            source_chars=len(raw),
            query_terms_count=len(terms),
        )
    selected: set[int] = set()
    used_chars = 0

    # Preserve the page title/lead, then fill the remaining budget with the
    # strongest blocks. The final sort restores the page's original order.
    for score, index, block in scored[:2]:
        if used_chars + len(block) + 2 <= max_chars:
            selected.add(index)
            used_chars += len(block) + 2

    for _, index, block in sorted(scored, key=lambda item: (-item[0], item[1])):
        if index in selected:
            continue
        # Once lexical matches exist, omit blocks that do not contain a query
        # term; the lead/title blocks were already preserved above.
        if matched and terms and not block_has_term(block, terms):
            continue
        if used_chars + len(block) + 2 > max_chars:
            continue
        selected.add(index)
        used_chars += len(block) + 2

    if not selected:
        selected.add(0)
    compacted = "\n\n".join(blocks[index] for index in sorted(selected)).strip()
    if len(compacted) > max_chars:
        compacted = compacted[:max_chars].rsplit(" ", 1)[0].rstrip()
    return compacted, {
        "mode": "query_relevant_blocks",
        "source_chars": len(raw),
        "selected_chars": len(compacted),
        "selected_blocks": len(selected),
        "matched_blocks": matched,
        "query_terms": len(terms),
    }


def firecrawl_fetch(url: str, max_chars: int) -> tuple[str, dict]:
    data, _ = request_json(
        "firecrawl",
        "https://api.firecrawl.dev/v2/scrape",
        method="POST",
        headers={"Authorization": f"Bearer {env_value('firecrawl') or ''}"},
        payload={"url": url, "formats": ["markdown"]},
        timeout=40,
    )
    body = data.get("data", data) if isinstance(data, dict) else {}
    content = body.get("markdown") or body.get("content") or body.get("text")
    if not content:
        raise ProviderError("firecrawl", 200, "no markdown content in response")
    return str(content)[:max_chars], {"metadata": body.get("metadata")}


def tavily_fetch(url: str, max_chars: int, query: str | None = None) -> tuple[str, dict]:
    payload = {
        "urls": [url],
        "extract_depth": "basic",
        "format": "markdown",
        "include_usage": True,
    }
    if query and query.strip():
        payload["query"] = query.strip()
        payload["chunks_per_source"] = 3
    data, _ = request_json(
        "tavily",
        "https://api.tavily.com/extract",
        method="POST",
        headers={"Authorization": f"Bearer {env_value('tavily') or ''}"},
        payload=payload,
        timeout=35,
    )
    rows = data.get("results", []) if isinstance(data, dict) else []
    if not rows:
        raise ProviderError("tavily", 200, "no extraction result")
    row = rows[0]
    content = row.get("raw_content") or row.get("content")
    if not content:
        raise ProviderError("tavily", 200, "no extracted content")
    return str(content)[:max_chars], {"usage": data.get("usage")}


def exa_fetch(url: str, max_chars: int) -> tuple[str, dict]:
    data, _ = request_json(
        "exa",
        "https://api.exa.ai/contents",
        method="POST",
        headers={"x-api-key": env_value("exa") or ""},
        payload={
            "ids": [url],
            "text": {"maxCharacters": max_chars},
        },
        timeout=35,
    )
    rows = data.get("results", []) if isinstance(data, dict) else []
    if not rows:
        raise ProviderError("exa", 200, "no content result")
    content = rows[0].get("text") or rows[0].get("content")
    if not content:
        raise ProviderError("exa", 200, "no extracted content")
    return str(content)[:max_chars], {}


def provider_candidates(requested: str) -> list[str]:
    if requested != "auto":
        return [requested]
    # Keep quota-bearing browser extraction as the last automatic fallback.
    # Static pages should use the local path first, then cheaper text APIs.
    candidates = ["direct"]
    if env_value("tavily"):
        candidates.append("tavily")
    if env_value("exa"):
        candidates.append("exa")
    if env_value("firecrawl"):
        candidates.append("firecrawl")
    return candidates


def ensure_fetch_tables(store) -> None:
    store.execute(
        "CREATE TABLE IF NOT EXISTS page_cache "
        "(cache_key TEXT PRIMARY KEY, created REAL NOT NULL, url TEXT NOT NULL, "
        "provider TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL, "
        "max_chars INTEGER NOT NULL)"
    )
    store.commit()


def normalized_fetch_url(url: str) -> str:
    return canonical_url(url) or url.strip()


def fetch_cache_key(url: str, requested: str, query: str | None = None) -> str:
    selected = {
        "cache_version": FETCH_CACHE_VERSION,
        "url": normalized_fetch_url(url),
        "provider": requested,
        "query": (query or "").strip().casefold(),
    }
    return hashlib.sha256(
        json.dumps(selected, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def fetch_cache_get(store, key: str, max_chars: int) -> dict | None:
    row = store.execute(
        "SELECT created, url, provider, content, metadata, max_chars "
        "FROM page_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    if time.time() - float(row[0]) > FETCH_CACHE_TTL or int(row[5]) < max_chars:
        if time.time() - float(row[0]) > FETCH_CACHE_TTL:
            store.execute("DELETE FROM page_cache WHERE cache_key = ?", (key,))
            store.commit()
        return None
    try:
        metadata = json.loads(row[4])
    except json.JSONDecodeError:
        metadata = {}
    return {
        "ok": True,
        "provider": row[2],
        "url": row[1],
        "content": row[3][:max_chars],
        "truncated": bool(
            isinstance(metadata.get("compaction"), dict)
            and metadata["compaction"].get("source_chars", 0) > len(row[3])
        ),
        "compacted": isinstance(metadata.get("compaction"), dict),
        "metadata": metadata,
        "errors": [],
        "cached": True,
    }


def fetch_cache_set(store, key: str, url: str, provider: str,
                    content: str, metadata: dict, max_chars: int) -> None:
    store.execute(
        "INSERT OR REPLACE INTO page_cache "
        "(cache_key, created, url, provider, content, metadata, max_chars) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            time.time(),
            normalized_fetch_url(url),
            provider,
            content,
            json.dumps(metadata or {}, ensure_ascii=False),
            max_chars,
        ),
    )
    store.commit()


def run(url: str, requested: str, max_chars: int, *, store=None,
        query: str | None = None,
        use_cache: bool = True) -> dict:
    normalized = normalized_fetch_url(url)
    source_chars = source_char_limit(max_chars)
    if store is not None:
        ensure_fetch_tables(store)
        if use_cache:
            cached = fetch_cache_get(
                store,
                fetch_cache_key(normalized, requested, query),
                max_chars,
            )
            if cached is not None:
                return cached

    errors = []
    for provider in provider_candidates(requested):
        try:
            if provider == "firecrawl":
                content, metadata = firecrawl_fetch(normalized, source_chars)
            elif provider == "tavily":
                content, metadata = tavily_fetch(normalized, source_chars, query)
            elif provider == "exa":
                content, metadata = exa_fetch(normalized, source_chars)
            else:
                content, metadata = direct_fetch(normalized, source_chars)
            placeholder_reason = dynamic_placeholder_reason(content)
            if placeholder_reason:
                raise ProviderError(provider, 200, placeholder_reason)
            compacted, compact_metadata = compact_content(content, query, max_chars)
            if not compacted:
                raise ProviderError(provider, 200, "no usable extracted content")
            metadata = dict(metadata or {})
            metadata["compaction"] = compact_metadata
            result = {
                "ok": True,
                "provider": provider,
                "url": normalized,
                "content": compacted,
                "truncated": len(content) >= source_chars,
                "compacted": len(compacted) < len(content),
                "metadata": metadata,
                "errors": errors,
                "cached": False,
            }
            if store is not None:
                ensure_fetch_tables(store)
                if use_cache:
                    fetch_cache_set(
                        store,
                        fetch_cache_key(normalized, requested, query),
                        normalized,
                        provider,
                        compacted,
                        metadata,
                        max_chars,
                    )
            return result
        except ProviderError as exc:
            errors.append({
                "provider": exc.provider,
                "status": exc.status,
                "message": exc.message,
            })
        except Exception as exc:
            errors.append({
                "provider": provider,
                "status": None,
                "message": f"{type(exc).__name__}: {exc}",
            })
    return {"ok": False, "url": normalized, "errors": errors}


def run_many(urls: list[str], requested: str, max_chars: int, max_pages: int,
             *, query: str | None = None,
             context_budget: int = DEFAULT_CONTEXT_BUDGET,
             use_cache: bool = True) -> dict:
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = normalized_fetch_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    selected = unique[:max_pages]
    skipped = unique[max_pages:]
    page_chars = min(max_chars, max(1000, context_budget // max(1, len(selected))))
    store = open_store()
    results = [
        run(
            url,
            requested,
            page_chars,
            store=store,
            query=query,
            use_cache=use_cache,
        )
        for url in selected
    ]
    if store is not None:
        store.close()
    return {
        "ok": bool(results) and all(item.get("ok") for item in results),
        "count": len(results),
        "max_pages": max_pages,
        "context_budget": context_budget,
        "context_chars": sum(len(item.get("content", "")) for item in results),
        "page_chars": page_chars if selected else 0,
        "skipped_urls": skipped,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", required=True,
                        help="URL to fetch; repeat for a bounded batch")
    parser.add_argument("--provider", choices=["auto", "firecrawl", "tavily", "exa", "direct"],
                        default="auto")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help="Maximum output characters per page after compaction")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help="Maximum pages for this invocation")
    parser.add_argument(
        "--fetch-budget", type=int, default=None,
        help="Deprecated alias for a per-invocation page cap; no historical budget is used",
    )
    parser.add_argument("--context-budget", type=int, default=DEFAULT_CONTEXT_BUDGET,
                        help="Maximum combined output characters for the batch")
    parser.add_argument("--query", required=True,
                        help="Original user question for relevance filtering")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    if (
        args.max_chars < 1
        or args.max_pages < 1
        or (args.fetch_budget is not None and args.fetch_budget < 1)
        or args.context_budget < 1
    ):
        parser.error(
            "--max-chars, --max-pages, --fetch-budget, and --context-budget must be positive"
        )
    effective_max_pages = args.max_pages
    if args.fetch_budget is not None:
        effective_max_pages = min(effective_max_pages, args.fetch_budget)
    output = run_many(
        args.url,
        args.provider,
        args.max_chars,
        effective_max_pages,
        query=args.query,
        context_budget=args.context_budget,
        use_cache=not args.no_cache,
    )
    if len(args.url) == 1 and output["count"] == 1 and not output["skipped_urls"]:
        output = output["results"][0]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
