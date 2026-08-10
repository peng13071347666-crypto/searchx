from __future__ import annotations

import json
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

from .engine import SearchEngine
from .fusion import canonical_url, domain_of


DEFAULT_BENCHMARK_PROVIDERS: dict[str, list[str]] = {
    "quick": ["serper", "brave", "tavily"],
    "web": ["serper", "brave", "tavily", "exa", "firecrawl"],
    "official": ["serper", "brave", "tavily", "exa"],
    "fresh": ["serper", "brave", "tavily", "exa"],
    "news": ["serper", "brave", "tavily", "exa", "newsapi", "firecrawl"],
    "code": ["serper", "brave", "tavily", "exa", "github", "firecrawl"],
    "academic": ["serper", "brave", "tavily", "exa", "firecrawl"],
    "cn": ["serper", "brave", "tavily", "exa", "baidu", "firecrawl"],
    "deep": ["serper", "brave", "tavily", "exa", "firecrawl"],
}
SUPPORTED_SCENARIOS = frozenset(DEFAULT_BENCHMARK_PROVIDERS)
SUPPORTED_BENCHMARK_PROVIDERS = frozenset(
    provider for candidates in DEFAULT_BENCHMARK_PROVIDERS.values() for provider in candidates
)
VALID_FRESHNESS = frozenset({"day", "week", "month", "year"})
FRESHNESS_FILTER_CAPABILITIES: dict[str, bool] = {
    "serper": True,
    "brave": True,
    "tavily": True,
    "newsapi": True,
    "baidu": True,
    "exa": False,
    "firecrawl": False,
    "github": False,
}
_FRESHNESS_WINDOWS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=31),
    "year": timedelta(days=365),
}
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)


@dataclass(slots=True)
class BenchmarkCase:
    id: str
    scenario: str
    query: str
    expected_domains: list[str] = field(default_factory=list)
    expected_terms: list[str] = field(default_factory=list)
    freshness: str | None = None
    providers: list[str] = field(default_factory=list)


def _is_valid_hostname(value: str) -> bool:
    return bool(_HOSTNAME_RE.fullmatch(value))


def load_cases(path: str | Path | None = None) -> list[BenchmarkCase]:
    if path is None:
        path = Path(__file__).with_name("data") / "benchmark.json"
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError("benchmark data must be valid JSON") from None
    if not isinstance(obj, list):
        raise ValueError("benchmark data must be a list of case objects")

    def required_text(row: dict[str, Any], field_name: str, index: int) -> str:
        value = row.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"benchmark case {index}: {field_name} must be a non-empty string")
        return value.strip()

    def string_list(row: dict[str, Any], field_name: str, index: int) -> list[str]:
        value = row.get(field_name, [])
        if not isinstance(value, list):
            raise ValueError(f"benchmark case {index}: {field_name} must be a list of non-empty strings")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"benchmark case {index}: {field_name} must be a list of non-empty strings")
        return [item.strip() for item in value]

    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(obj, 1):
        if not isinstance(row, dict):
            raise ValueError(f"benchmark case {index}: case must be an object")
        case_id = required_text(row, "id", index)
        scenario = required_text(row, "scenario", index)
        query = required_text(row, "query", index)
        if case_id in seen_ids:
            raise ValueError(f"benchmark case {index}: duplicate id {case_id!r}")
        if scenario not in SUPPORTED_SCENARIOS:
            raise ValueError(f"benchmark case {index}: unsupported scenario {scenario!r}")
        freshness = row.get("freshness")
        if freshness is not None and (not isinstance(freshness, str) or freshness not in VALID_FRESHNESS):
            raise ValueError(f"benchmark case {index}: freshness must be one of {sorted(VALID_FRESHNESS)}")
        if scenario in {"fresh", "news"} and freshness is None:
            raise ValueError(f"benchmark case {index}: freshness is required for {scenario!r} cases")

        raw_expected_domains = row.get("expected_domains", [])
        expected_domains = string_list(row, "expected_domains", index)
        if any(
            raw_domain != raw_domain.strip() or not _is_valid_hostname(domain)
            for raw_domain, domain in zip(raw_expected_domains, expected_domains, strict=True)
        ):
            raise ValueError(f"benchmark case {index}: expected_domains must contain hostnames only")

        providers = string_list(row, "providers", index)
        unknown_providers = sorted(set(providers) - SUPPORTED_BENCHMARK_PROVIDERS)
        if unknown_providers:
            raise ValueError(f"benchmark case {index}: unsupported provider(s) {unknown_providers}")
        providers = list(dict.fromkeys(providers))

        cases.append(
            BenchmarkCase(
                id=case_id,
                scenario=scenario,
                query=query,
                expected_domains=expected_domains,
                expected_terms=string_list(row, "expected_terms", index),
                freshness=freshness,
                providers=providers,
            )
        )
        seen_ids.add(case_id)
    return cases


def _normalize_text(text: str) -> str:
    return " ".join((text or "").lower().replace("_", " ").split())


def _expected_domain_score(results: list[dict[str, Any]], domains: list[str]) -> float:
    if not domains:
        return 0.0
    expected = [domain.lower().removeprefix("www.") for domain in domains]
    best = 0.0
    for index, result in enumerate(results[:10], 1):
        domain = domain_of(result.get("url", ""))
        if any(domain == target or domain.endswith("." + target) for target in expected):
            best = max(best, 1.0 / math.log2(index + 1))
    return min(best, 1.0)


def _term_score(results: list[dict[str, Any]], terms: list[str]) -> float:
    if not terms:
        return 0.0
    joined = " ".join(
        _normalize_text((result.get("title") or "") + " " + (result.get("snippet") or ""))
        for result in results[:5]
    )
    hits = sum(1 for term in terms if _normalize_text(term) in joined)
    return hits / max(1, len(terms))


def _parse_published_at(value: Any) -> datetime | None:
    """Parse standard timestamps without accepting provider-specific relative prose."""
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


def _freshness_metrics(
    results: list[dict[str, Any]],
    freshness: str | None,
    reference_time: datetime,
) -> dict[str, float | None]:
    if freshness is None:
        return {
            "dated_result_coverage": None,
            "stale_ratio": None,
            "freshness_score": None,
        }
    window = _FRESHNESS_WINDOWS[freshness]
    cutoff = reference_time - window
    dated = 0
    fresh = 0
    stale = 0
    for result in results:
        published_at = _parse_published_at(result.get("published_at"))
        if published_at is None:
            continue
        dated += 1
        if cutoff <= published_at <= reference_time:
            fresh += 1
        else:
            stale += 1
    total = len(results)
    return {
        "dated_result_coverage": round(dated / total, 4) if total else 0.0,
        "stale_ratio": round(stale / dated, 4) if dated else None,
        # This intentionally includes undated results in the denominator: they
        # are measurable but never receive freshness credit.
        "freshness_score": round(fresh / total, 4) if total else 0.0,
    }


def _result_quality(
    results: list[dict[str, Any]],
    case: BenchmarkCase,
    freshness_metrics: Mapping[str, float | None] | None = None,
) -> dict[str, float]:
    freshness_score = float((freshness_metrics or {}).get("freshness_score") or 0.0)
    if not results:
        return {
            "domain_hit": 0.0,
            "term_hit": 0.0,
            "diversity": 0.0,
            "snippet": 0.0,
            "metadata": 0.0,
            "freshness_score": round(freshness_score, 4),
            "quality": 0.0,
        }
    domains = [domain_of(result.get("url", "")) for result in results[:10] if domain_of(result.get("url", ""))]
    diversity = len(set(domains)) / max(1, min(10, len(results)))
    snippet_lengths = [len((result.get("snippet") or "").strip()) for result in results[:5]]
    snippet = min(statistics.fmean(snippet_lengths) / 500.0, 1.0) if snippet_lengths else 0.0
    metadata_hits = sum(_parse_published_at(result.get("published_at")) is not None for result in results[:10])
    metadata = metadata_hits / max(1, min(10, len(results)))
    domain_hit = _expected_domain_score(results, case.expected_domains)
    term_hit = _term_score(results, case.expected_terms)

    if case.scenario in {"fresh", "news"} and case.freshness:
        # Fresh/news runs preserve the durable relevance anchors but reserve a
        # meaningful share for dated, in-window evidence.
        if case.expected_domains or case.expected_terms:
            quality = (
                0.35 * domain_hit
                + 0.20 * term_hit
                + 0.10 * diversity
                + 0.10 * snippet
                + 0.05 * metadata
                + 0.20 * freshness_score
            )
        else:
            quality = 0.35 * diversity + 0.25 * snippet + 0.15 * metadata + 0.25 * freshness_score
    elif case.expected_domains or case.expected_terms:
        quality = 0.45 * domain_hit + 0.25 * term_hit + 0.15 * diversity + 0.10 * snippet + 0.05 * metadata
    else:
        quality = 0.45 * diversity + 0.35 * snippet + 0.20 * metadata
    return {
        "domain_hit": round(domain_hit, 4),
        "term_hit": round(term_hit, 4),
        "diversity": round(diversity, 4),
        "snippet": round(snippet, 4),
        "metadata": round(metadata, 4),
        "freshness_score": round(freshness_score, 4),
        "quality": round(quality, 4),
    }


def _provider_options(case: BenchmarkCase, provider: str) -> dict[str, Any]:
    opts: dict[str, Any] = {"mode": case.scenario, "limit": 10}
    if case.freshness:
        opts["freshness"] = case.freshness
    if case.scenario == "news":
        opts["mode"] = "news"
    elif case.scenario == "academic":
        opts["mode"] = "academic"
        if provider == "exa":
            opts["depth"] = "auto"
        elif provider == "tavily":
            opts["depth"] = "advanced"
    elif case.scenario == "code" and provider == "github":
        lowered = case.query.lower()
        if "issue" in lowered or "bug" in lowered:
            opts["category"] = "issues"
        elif "source" in lowered or "implementation" in lowered:
            opts["category"] = "code"
        else:
            opts["category"] = "repositories"
    elif case.scenario == "deep":
        if provider == "exa":
            opts["depth"] = "deep-lite"
        elif provider == "tavily":
            opts["depth"] = "advanced"
    return opts


def _validated_filter(
    values: set[str] | None,
    supported: frozenset[str],
    label: str,
) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (set, frozenset, list, tuple)):
        raise ValueError(f"{label} filter must be a collection of supported names")
    try:
        selected = set(values)
    except TypeError:
        raise ValueError(f"{label} filter must contain hashable supported names") from None
    unknown = sorted(str(value) for value in selected if value not in supported)
    if unknown:
        raise ValueError(f"unknown benchmark {label}(s): {', '.join(unknown)}")
    return selected


def _validate_case_for_run(case: BenchmarkCase) -> None:
    if not isinstance(case.scenario, str) or case.scenario not in SUPPORTED_SCENARIOS:
        raise ValueError(f"benchmark case {case.id!r}: unsupported scenario {case.scenario!r}")
    if case.freshness is not None and (
        not isinstance(case.freshness, str) or case.freshness not in VALID_FRESHNESS
    ):
        raise ValueError(f"benchmark case {case.id!r}: invalid freshness")
    if case.scenario in {"fresh", "news"} and case.freshness is None:
        raise ValueError(f"benchmark case {case.id!r}: freshness is required for {case.scenario!r} cases")
    if not isinstance(case.providers, list) or any(not isinstance(provider, str) for provider in case.providers):
        raise ValueError(f"benchmark case {case.id!r}: providers must be a list of provider names")
    unknown_providers = sorted(set(case.providers) - SUPPORTED_BENCHMARK_PROVIDERS)
    if unknown_providers:
        raise ValueError(f"benchmark case {case.id!r}: unsupported provider(s) {unknown_providers}")


class BenchmarkRunner:
    def __init__(self, engine: SearchEngine) -> None:
        self.engine = engine

    def run(
        self,
        cases: list[BenchmarkCase],
        *,
        scenarios: set[str] | None = None,
        providers: set[str] | None = None,
        max_cases: int | None = None,
        workers: int = 4,
    ) -> dict[str, Any]:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError("workers must be a positive integer")
        for case in cases:
            _validate_case_for_run(case)
        scenario_filter = _validated_filter(scenarios, SUPPORTED_SCENARIOS, "scenario")
        provider_filter = _validated_filter(providers, SUPPORTED_BENCHMARK_PROVIDERS, "provider")
        selected = [case for case in cases if not scenario_filter or case.scenario in scenario_filter]
        if max_cases is not None:
            if isinstance(max_cases, bool) or not isinstance(max_cases, int) or max_cases < 0:
                raise ValueError("max_cases must be a non-negative integer")
            selected = selected[:max_cases]
        configured = set(self.engine.configured_providers())
        tasks: list[tuple[BenchmarkCase, str]] = []
        skipped_unconfigured_call_count = 0
        for case in selected:
            names = list(dict.fromkeys(case.providers or DEFAULT_BENCHMARK_PROVIDERS.get(case.scenario, [])))
            for provider in names:
                if provider_filter and provider not in provider_filter:
                    continue
                if provider not in configured:
                    skipped_unconfigured_call_count += 1
                    continue
                tasks.append((case, provider))

        started = time.time()
        reference_time = datetime.now(UTC)
        rows: list[dict[str, Any]] = []

        def report_case_fields(case: BenchmarkCase, provider: str) -> dict[str, Any]:
            return {
                "expected_domains": list(case.expected_domains),
                "expected_terms": list(case.expected_terms),
                "freshness": case.freshness,
                "freshness_filter_supported": FRESHNESS_FILTER_CAPABILITIES.get(provider, False),
            }

        def run_one(case: BenchmarkCase, provider: str) -> dict[str, Any]:
            request_options = _provider_options(case, provider)
            call = self.engine.provider_search(provider, case.query, **request_options)
            data = call.to_dict(include_results=True)
            results = data.get("results", [])
            if not isinstance(results, list):
                results = []
            freshness = _freshness_metrics(results, case.freshness, reference_time)
            quality = _result_quality(results, case, freshness)
            top_urls = [
                canonical_url(result.get("url", ""))
                for result in results[:10]
                if isinstance(result, Mapping) and result.get("url")
            ]
            return {
                "case_id": case.id,
                "scenario": case.scenario,
                "query": case.query,
                "provider": provider,
                "status": call.status,
                "http_status": call.http_status,
                "elapsed_ms": round(call.elapsed_ms, 2),
                "result_count": len(call.results),
                "quality": quality,
                "usage": call.usage,
                "metadata": call.metadata,
                "request_options": request_options,
                "error": call.error,
                "top_urls": top_urls,
                "results": results,
                **report_case_fields(case, provider),
                **freshness,
            }

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_one, case, provider): (case, provider) for case, provider in tasks}
            for future in as_completed(futures):
                try:
                    rows.append(future.result())
                except Exception as exc:
                    case, provider = futures[future]
                    freshness = _freshness_metrics([], case.freshness, reference_time)
                    rows.append(
                        {
                            "case_id": case.id,
                            "scenario": case.scenario,
                            "query": case.query,
                            "provider": provider,
                            "status": "error",
                            "http_status": None,
                            "elapsed_ms": 0.0,
                            "result_count": 0,
                            "quality": _result_quality([], case, freshness),
                            "usage": {},
                            "metadata": {},
                            "request_options": _provider_options(case, provider),
                            "error": str(exc),
                            "top_urls": [],
                            "results": [],
                            **report_case_fields(case, provider),
                            **freshness,
                        }
                    )
        rows.sort(key=lambda row: (row["case_id"], row["provider"]))
        self._add_unique_recall(rows)
        summary = summarize_rows(rows)
        report: dict[str, Any] = {
            "version": 1,
            "started_at": started,
            "reference_time": reference_time.isoformat(),
            "elapsed_seconds": round(time.time() - started, 2),
            "case_count": len(selected),
            "selected_case_ids": [case.id for case in selected],
            "configured_providers": sorted(configured),
            "scheduled_call_count": len(tasks),
            "skipped_unconfigured_call_count": skipped_unconfigured_call_count,
            "call_count": len(rows),
            "rows": rows,
            "summary": summary,
        }
        if provider_filter:
            report["requested_provider_filter"] = sorted(provider_filter)
        return report

    @staticmethod
    def _add_unique_recall(rows: list[dict[str, Any]]) -> None:
        def canonical_urls(row: Mapping[str, Any]) -> list[str]:
            normalized: list[str] = []
            for value in row.get("top_urls", []):
                if not isinstance(value, str):
                    continue
                url = canonical_url(value)
                if url and domain_of(url):
                    normalized.append(url)
            return normalized

        by_case: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_case.setdefault(row["case_id"], []).append(row)
        for case_rows in by_case.values():
            successful = [
                row
                for row in case_rows
                if row.get("status") == "ok" and canonical_urls(row)
            ]
            comparison_provider_count = len({str(row.get("provider", "")) for row in successful})
            url_to_providers: dict[str, set[str]] = {}
            for row in successful:
                for url in canonical_urls(row):
                    url_to_providers.setdefault(url, set()).add(str(row.get("provider", "")))
            for row in case_rows:
                row["comparison_provider_count"] = comparison_provider_count
                urls = canonical_urls(row)
                if row.get("status") != "ok" or comparison_provider_count < 2 or not urls:
                    row["unique_ratio"] = None
                    continue
                unique = sum(1 for url in urls if len(url_to_providers.get(url, set())) == 1)
                row["unique_ratio"] = round(unique / max(1, len(urls)), 4)


def _finite_numbers(values: list[Any]) -> list[float]:
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numbers.append(number)
    return numbers


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["scenario"], row["provider"]), []).append(row)
    summary: dict[str, dict[str, Any]] = {}
    for (scenario, provider), items in grouped.items():
        success = [row for row in items if row.get("status") == "ok"]
        quality = _finite_numbers([row.get("quality", {}).get("quality") for row in success])
        latency = _finite_numbers([row.get("elapsed_ms") for row in success])
        unique = _finite_numbers([row.get("unique_ratio") for row in success])
        result_count = _finite_numbers([row.get("result_count") for row in success])
        dated_coverage = _finite_numbers([row.get("dated_result_coverage") for row in success])
        stale_ratio = _finite_numbers([row.get("stale_ratio") for row in success])
        freshness_score = _finite_numbers([row.get("freshness_score") for row in success])
        comparison_count = _finite_numbers([row.get("comparison_provider_count") for row in success])
        capabilities = [row.get("freshness_filter_supported") for row in items]
        support_values = [value for value in capabilities if isinstance(value, bool)]
        summary.setdefault(scenario, {})[provider] = {
            "calls": len(items),
            "success_rate": round(len(success) / max(1, len(items)), 4),
            "quality": round(statistics.fmean(quality), 4) if quality else 0.0,
            "latency_ms": round(statistics.median(latency), 2) if latency else 0.0,
            "unique_ratio": round(statistics.fmean(unique), 4) if unique else None,
            "comparison_provider_count": round(statistics.fmean(comparison_count), 2) if comparison_count else 0.0,
            "avg_results": round(statistics.fmean(result_count), 2) if result_count else 0.0,
            "dated_result_coverage": round(statistics.fmean(dated_coverage), 4) if dated_coverage else None,
            "stale_ratio": round(statistics.fmean(stale_ratio), 4) if stale_ratio else None,
            "freshness_score": round(statistics.fmean(freshness_score), 4) if freshness_score else None,
            "freshness_filter_supported": all(support_values) if support_values else None,
        }
    return summary


_CORE_TUNING_METRIC_RANGES: dict[str, tuple[float, float | None]] = {
    "success_rate": (0.0, 1.0),
    "quality": (0.0, 1.0),
    "latency_ms": (0.0, None),
}
_OPTIONAL_TUNING_METRIC_RANGES: dict[str, tuple[float, float | None]] = {
    "unique_ratio": (0.0, 1.0),
    "dated_result_coverage": (0.0, 1.0),
    "stale_ratio": (0.0, 1.0),
    "freshness_score": (0.0, 1.0),
    "comparison_provider_count": (0.0, None),
    "calls": (0.0, None),
}


def _bounded_metric(value: Any, minimum: float, maximum: float | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        return None
    return number


def _metrics_are_structurally_valid(metrics: Mapping[str, Any]) -> bool:
    for key, (minimum, maximum) in _CORE_TUNING_METRIC_RANGES.items():
        if _bounded_metric(metrics.get(key), minimum, maximum) is None:
            return False
    for key, (minimum, maximum) in _OPTIONAL_TUNING_METRIC_RANGES.items():
        if key in metrics and metrics[key] is not None and _bounded_metric(metrics[key], minimum, maximum) is None:
            return False
    capability = metrics.get("freshness_filter_supported")
    return capability is None or isinstance(capability, bool)


def _metrics_are_valid(metrics: Mapping[str, Any]) -> bool:
    if not _metrics_are_structurally_valid(metrics):
        return False
    avg_results = _bounded_metric(metrics.get("avg_results"), 0.0, None)
    return avg_results is not None and avg_results > 0.0


def _eligible_tuning_entry(
    scenario: str,
    provider: str,
    metrics: Mapping[str, Any],
) -> tuple[float, float] | None:
    if scenario not in SUPPORTED_SCENARIOS or provider not in SUPPORTED_BENCHMARK_PROVIDERS:
        return None
    if not _metrics_are_valid(metrics):
        return None
    success = _bounded_metric(metrics["success_rate"], 0.0, 1.0)
    quality = _bounded_metric(metrics["quality"], 0.0, 1.0)
    latency = _bounded_metric(metrics["latency_ms"], 0.0, None)
    if success is None or quality is None or latency is None or success < 0.5:
        return None
    unique = 0.0
    if metrics.get("unique_ratio") is not None:
        unique = _bounded_metric(metrics["unique_ratio"], 0.0, 1.0) or 0.0
    if scenario in {"fresh", "news"}:
        # Both sources must agree: a report cannot claim support for an adapter
        # known not to accept this filter, and an eligible adapter must have
        # actually reported support for this run.
        if not FRESHNESS_FILTER_CAPABILITIES.get(provider, False):
            return None
        if metrics.get("freshness_filter_supported") is not True:
            return None
    latency_score = 1.0 / (1.0 + latency / 1800.0)
    score = 0.55 * quality + 0.15 * unique + 0.20 * success + 0.10 * latency_score
    return score, success


def tune_profile(report: Mapping[str, Any]) -> dict[str, Any]:
    summary = report.get("summary")
    routes: dict[str, Any] = {}
    mode_weights: dict[str, dict[str, float]] = {}
    if not isinstance(summary, Mapping):
        summary = {}
    for scenario, provider_map in summary.items():
        if not isinstance(scenario, str) or scenario not in SUPPORTED_SCENARIOS or not isinstance(provider_map, Mapping):
            continue
        eligible: list[tuple[str, float]] = []
        for provider, metrics in provider_map.items():
            if not isinstance(provider, str) or not isinstance(metrics, Mapping):
                continue
            entry = _eligible_tuning_entry(scenario, provider, metrics)
            if entry is not None:
                eligible.append((provider, entry[0]))
        eligible.sort(key=lambda item: (-item[1], item[0]))
        if not eligible:
            continue
        primary = [provider for provider, _ in eligible[:3]]
        fallback = [provider for provider, _ in eligible[3:]]
        best = max(eligible[0][1], 0.0001)
        mode_weights[scenario] = {
            provider: round(max(0.55, min(1.45, score / best)), 3) for provider, score in eligible
        }
        routes[scenario] = {
            "primary": primary,
            "fallback": fallback,
            "min_results": 8 if scenario != "deep" else 12,
        }
    return {
        "version": 1,
        "generated_from": "searchx benchmark",
        "routes": routes,
        "mode_provider_weights": mode_weights,
    }


def validate_tunable_report(report: Mapping[str, Any]) -> None:
    """Ensure the CLI never turns an empty, malformed, or failed run into a profile."""
    call_count = report.get("call_count")
    rows = report.get("rows")
    if isinstance(call_count, bool) or not isinstance(call_count, int) or call_count <= 0:
        raise ValueError("benchmark report has no calls to tune")
    if not isinstance(rows, list) or not rows:
        raise ValueError("benchmark report has no calls to tune")
    if not any(isinstance(row, Mapping) and row.get("status") == "ok" for row in rows):
        raise ValueError("benchmark report has no successful calls to tune")

    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("benchmark report has no usable routes")
    for scenario, provider_map in summary.items():
        if not isinstance(scenario, str) or scenario not in SUPPORTED_SCENARIOS:
            raise ValueError("benchmark report contains an unknown scenario")
        if not isinstance(provider_map, Mapping):
            raise ValueError("benchmark report contains an invalid provider summary")
        for provider, metrics in provider_map.items():
            if not isinstance(provider, str) or provider not in SUPPORTED_BENCHMARK_PROVIDERS:
                raise ValueError("benchmark report contains an unknown provider")
            # A bad average means this provider supplied no usable results; it
            # is ineligible, but does not corrupt a report that also has a
            # healthy candidate. Other malformed metrics remain report errors.
            if not isinstance(metrics, Mapping) or not _metrics_are_structurally_valid(metrics):
                raise ValueError("benchmark report contains invalid provider metrics")
    if not tune_profile(report)["routes"]:
        raise ValueError("benchmark report has no usable routes")


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "version": report.get("version"),
        "elapsed_seconds": report.get("elapsed_seconds"),
        "case_count": report.get("case_count"),
        "call_count": report.get("call_count"),
        "summary": report.get("summary"),
    }
    for key in (
        "selected_case_ids",
        "configured_providers",
        "requested_provider_filter",
        "scheduled_call_count",
        "skipped_unconfigured_call_count",
        "reference_time",
    ):
        if key in report:
            compact[key] = report[key]
    return compact
