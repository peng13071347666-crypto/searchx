from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

from .fusion import domain_of
from .models import SearchResult


def parse_standard_timestamp(value: object) -> datetime | None:
    """Parse ISO-8601/RFC timestamps only; relative provider prose stays unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (OverflowError, TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(text)
        except (IndexError, OverflowError, TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _text_features(value: object) -> tuple[bool, int]:
    if not isinstance(value, str):
        return False, 0
    text = value.strip()
    return bool(text), len(text)


def _matched_providers(result: SearchResult) -> list[str]:
    raw = result.metadata.get("matched_providers")
    if isinstance(raw, list):
        names = [name for name in raw if isinstance(name, str) and name]
    else:
        names = [name for name in result.provider.split("+") if name]
    return list(dict.fromkeys(names))


def build_evidence_signals(
    result: SearchResult,
    successful_providers: Iterable[str],
    *,
    reference_time: datetime,
) -> dict[str, Any]:
    """Build descriptive, non-truth evidence signals for one fused result."""
    successful = list(dict.fromkeys(name for name in successful_providers if isinstance(name, str) and name))
    matched = _matched_providers(result)
    parsed = parse_standard_timestamp(result.published_at)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)
    else:
        reference_time = reference_time.astimezone(UTC)
    title_present, title_length = _text_features(result.title)
    snippet_present, snippet_length = _text_features(result.snippet)
    author_present, author_length = _text_features(result.author)
    content_present, content_length = _text_features(result.content)
    participating_count = len(successful)
    matched_count = len(matched)
    return {
        "verification_status": "not_verified",
        "discovery_provider_count": matched_count,
        "matched_provider_count": matched_count,
        "participating_successful_provider_count": participating_count,
        "provider_agreement_ratio": round(matched_count / participating_count, 4) if participating_count else 0.0,
        "source_domain": domain_of(result.url),
        "title_present": title_present,
        "title_length": title_length,
        "snippet_present": snippet_present,
        "snippet_length": snippet_length,
        "author_present": author_present,
        "author_length": author_length,
        "content_present": content_present,
        "content_length": content_length,
        "published_at_raw": result.published_at if isinstance(result.published_at, str) else None,
        "published_at_parse_status": "parsed" if parsed is not None else "unknown",
        "published_at_normalized": parsed.isoformat() if parsed is not None else None,
        "published_at_age_seconds": round((reference_time - parsed).total_seconds(), 3) if parsed is not None else None,
    }


def annotate_evidence(
    results: Iterable[SearchResult],
    successful_providers: Iterable[str],
    *,
    reference_time: datetime,
) -> None:
    """Attach only descriptive evidence metadata while preserving result order/content."""
    provider_names = list(successful_providers)
    for result in results:
        result.metadata["evidence"] = build_evidence_signals(
            result,
            provider_names,
            reference_time=reference_time,
        )
