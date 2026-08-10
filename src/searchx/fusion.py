from __future__ import annotations

import posixpath
import re
import urllib.parse
from collections import defaultdict
from typing import Iterable

from .models import ProviderCall, SearchResult


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "ref",
    "ref_src",
    "mc_cid",
    "mc_eid",
}


def canonical_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url.strip())
        scheme = (parts.scheme or "https").lower()
        host = (parts.hostname or "").lower().removeprefix("www.")
        if not host:
            return url.strip()
        port = parts.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        path = parts.path or "/"
        path = posixpath.normpath(path)
        if parts.path.endswith("/") and not path.endswith("/"):
            path += "/"
        if path != "/":
            path = path.rstrip("/")
        query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        clean_pairs = [(k, v) for k, v in query_pairs if k.lower() not in TRACKING_PARAMS]
        clean_pairs.sort()
        query = urllib.parse.urlencode(clean_pairs, doseq=True)
        return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))
    except (ValueError, UnicodeError):
        return url.strip()


def domain_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fuse_calls(
    calls: Iterable[ProviderCall],
    *,
    provider_weights: dict[str, float] | None = None,
    rrf_k: int = 60,
    limit: int = 10,
    domain_cap: int = 2,
) -> list[SearchResult]:
    weights = provider_weights or {}
    entries: dict[str, dict] = {}
    for call in calls:
        if not call.ok:
            continue
        weight = float(weights.get(call.provider, 1.0))
        for result in call.results:
            key = canonical_url(result.url)
            if not key:
                continue
            rank = max(1, result.rank or 1)
            score = weight / (rrf_k + rank)
            entry = entries.get(key)
            if entry is None:
                entry = {
                    "score": 0.0,
                    "result": result,
                    "providers": [],
                    "ranks": {},
                    "provider_scores": {},
                    "snippets": [],
                }
                entries[key] = entry
            entry["score"] += score
            entry["providers"].append(call.provider)
            entry["ranks"][call.provider] = rank
            if result.provider_score is not None:
                entry["provider_scores"][call.provider] = result.provider_score
            if result.snippet:
                entry["snippets"].append(result.snippet)
            current: SearchResult = entry["result"]
            if len(_clean_text(result.snippet)) > len(_clean_text(current.snippet)):
                current.snippet = result.snippet
            if not current.published_at and result.published_at:
                current.published_at = result.published_at
            if not current.author and result.author:
                current.author = result.author
            if not current.content and result.content:
                current.content = result.content

    ordered = sorted(entries.items(), key=lambda item: (-item[1]["score"], item[0]))
    domain_counts: dict[str, int] = defaultdict(int)
    fused: list[SearchResult] = []
    for url, entry in ordered:
        result: SearchResult = entry["result"]
        domain = domain_of(url)
        if domain and domain_cap > 0 and domain_counts[domain] >= domain_cap:
            continue
        if domain:
            domain_counts[domain] += 1
        result.url = url
        providers = sorted(set(entry["providers"]))
        result.metadata = {
            **result.metadata,
            "rrf_score": round(entry["score"], 8),
            "matched_providers": providers,
            "provider_ranks": entry["ranks"],
            "provider_scores": entry["provider_scores"],
            "domain": domain,
        }
        result.provider = "+".join(providers)
        result.rank = len(fused) + 1
        fused.append(result)
        if len(fused) >= limit:
            break
    return fused

