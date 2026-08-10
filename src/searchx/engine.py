from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import Settings
from .evidence import annotate_evidence
from .fusion import canonical_url, domain_of, fuse_calls
from .http import HttpClient
from .models import (
    FetchAttempt,
    FetchOutcome,
    ProviderCall,
    SearchPlan,
    SearchResponse,
    redact_sensitive_text,
    sanitize_sensitive_value,
)
from .providers import BaseProvider, build_providers
from .router import Router


_FETCH_CANDIDATES = ("firecrawl", "tavily", "exa")
_FETCH_METHODS = {
    "firecrawl": "scrape",
    "tavily": "extract",
    "exa": "contents",
}

SEARCH_INTENSITIES = ("quick", "adaptive", "deep")
_DEFAULT_INTENSITY_BUDGETS = {
    "quick": {"max_provider_calls": 2, "max_stages": 2},
    "adaptive": {"max_provider_calls": 4, "max_stages": 3},
    "deep": {"max_provider_calls": None, "max_stages": 2},
}


def _validate_query(query: object) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    return query


def _validate_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    return url


def _positive_budget(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _normalize_intensity(value: object) -> str:
    if not isinstance(value, str) or value.lower() not in SEARCH_INTENSITIES:
        raise ValueError("intensity must be one of: quick, adaptive, deep")
    return value.lower()


def _resolved_search_budget(
    plan: SearchPlan,
    intensity: str,
    *,
    max_provider_calls: int | None,
    max_stages: int | None,
    all_fallbacks: bool = False,
) -> tuple[int, int]:
    ordered = list(dict.fromkeys(plan.primary + plan.fallback))
    route_size = max(1, len(ordered))
    defaults = _DEFAULT_INTENSITY_BUDGETS[intensity]
    default_calls = defaults["max_provider_calls"]
    if default_calls is None or all_fallbacks:
        default_calls = route_size
    default_stages = defaults["max_stages"]
    if all_fallbacks and intensity == "quick":
        default_stages = route_size
    return (
        min(route_size, max_provider_calls if max_provider_calls is not None else int(default_calls)),
        max_stages if max_stages is not None else int(default_stages),
    )


def _execution_stages(plan: SearchPlan, intensity: str, configured: set[str]) -> list[list[str]]:
    primary = [name for name in dict.fromkeys(plan.primary) if name in configured]
    primary_set = set(primary)
    fallback = [
        name
        for name in dict.fromkeys(plan.fallback)
        if name in configured and name not in primary_set
    ]
    if intensity == "quick":
        return [[name] for name in primary + fallback]
    if intensity == "adaptive":
        stages: list[list[str]] = []
        if primary:
            stages.append(primary[:1])
        if len(primary) > 1:
            stages.append(primary[1:])
        if fallback:
            stages.append(fallback)
        return stages
    return [stage for stage in (primary, fallback) if stage]


def _fused_progress(
    calls: list[ProviderCall],
    *,
    provider_weights: dict[str, float],
    rrf_k: int,
    limit: int,
    domain_cap: int,
) -> tuple[list[Any], int]:
    results = fuse_calls(
        deepcopy(calls),
        provider_weights=provider_weights,
        rrf_k=rrf_k,
        limit=limit,
        domain_cap=domain_cap,
    )
    domains = {domain_of(result.url) for result in results if domain_of(result.url)}
    return results, len(domains)


def _payload_elapsed_ms(payload: Any, fallback: float) -> float:
    if isinstance(payload, Mapping):
        value = payload.get("elapsed_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0:
            return float(value)
    return fallback


def _payload_http_status(payload: Any) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("http_status", "status_code", "status"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _payload_failed(payload: Any) -> bool:
    return isinstance(payload, Mapping) and (payload.get("ok") is False or payload.get("success") is False)


def _payload_error(payload: Any, *, default: str = "provider reported an unsuccessful fetch payload") -> str:
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), str) and payload["error"].strip():
        return payload["error"].strip()
    return default


def _normalized_content(payload: Any) -> dict[str, str | None]:
    """Extract an adapter-neutral content shape without claiming completeness."""

    def find(value: Any, depth: int = 0) -> tuple[str, str] | None:
        if depth > 4:
            return None
        if isinstance(value, Mapping):
            for key, content_format in (
                ("markdown", "markdown"),
                ("raw_content", "markdown"),
                ("content", "text"),
                ("text", "text"),
                ("html", "html"),
            ):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return content_format, text
            for key in ("data", "result", "results", "documents"):
                nested = value.get(key)
                found = find(nested, depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                found = find(item, depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, str) and value.strip():
            return "text", value
        return None

    found = find(payload)
    if found is None:
        return {"format": "unknown", "text": None}
    return {"format": found[0], "text": found[1]}


def _fetch_payload_failure(payload: Any) -> str | None:
    """Return a stable failure reason unless a fetch payload has usable text."""
    if not isinstance(payload, Mapping) or not payload:
        return "provider returned no usable fetch payload"
    if _payload_failed(payload):
        return _payload_error(payload)
    http_status = _payload_http_status(payload)
    if http_status is not None and http_status >= 400:
        return _payload_error(payload, default=f"provider returned HTTP {http_status}")
    if _normalized_content(payload)["text"] is None:
        return "provider returned no extractable fetch content"
    return None


def build_research_plan(
    query: str,
    plan: SearchPlan,
    *,
    intensity: str = "adaptive",
    max_provider_calls: int | None = None,
    max_stages: int | None = None,
) -> dict[str, Any]:
    """Return a fixed offline workflow; it never calls a provider or planner."""
    normalized_intensity = _normalize_intensity(intensity)
    call_budget = _positive_budget(max_provider_calls, "max_provider_calls")
    stage_budget = _positive_budget(max_stages, "max_stages")
    resolved_calls, resolved_stages = _resolved_search_budget(
        plan,
        normalized_intensity,
        max_provider_calls=call_budget,
        max_stages=stage_budget,
    )
    target_results = max(0, int(plan.min_results))
    target_domains = min(2, target_results)
    return {
        "query": query,
        "verification_status": "not_verified",
        "plan": plan.to_dict(),
        "search_strategy": {
            "intensity": normalized_intensity,
            "max_provider_calls": resolved_calls,
            "max_stages": resolved_stages,
            "stage_policy": {
                "quick": "one provider at a time; stop at the first usable result",
                "adaptive": "start with one provider and escalate only for a named evidence gap",
                "deep": "run primary and fallback stages within the hard budget",
            }[normalized_intensity],
            "success_criteria": {
                "minimum_usable_results": target_results,
                "minimum_distinct_domains": target_domains,
            },
            "hard_rule": "the engine enforces budgets; the caller may request escalation but may not exceed them",
        },
        "workflow": [
            {
                "step": "search",
                "max_runs": 1,
                "max_stages": resolved_stages,
                "max_provider_calls": resolved_calls,
            },
            {"step": "evidence-gap-check", "max_passes": 1},
            {"step": "optional-fetch", "max_urls": 3, "condition": "only for selected URLs"},
            {"step": "cross-check", "max_passes": 1},
        ],
        "stop_criteria": {
            "max_search_runs": 1,
            "max_search_stages": resolved_stages,
            "max_provider_calls": resolved_calls,
            "max_optional_fetches": 3,
            "max_cross_check_passes": 1,
            "completion": "stop when evidence criteria are met or any hard budget is exhausted; never assert verification from discovery alone",
        },
    }


class SearchEngine:
    def __init__(self, settings: Settings | None = None, profile_path: str | None = None) -> None:
        self.settings = settings or Settings.load(profile_path)
        self.http = HttpClient(timeout=self.settings.timeout, retries=self.settings.retries)
        self.providers = build_providers(self.http)
        self.router = Router(self.settings, profile_path)

    def configured_providers(self) -> list[str]:
        return [name for name, provider in self.providers.items() if provider.configured]

    def provider_search(self, provider: str, query: str, **kwargs: Any) -> ProviderCall:
        valid_query = _validate_query(query)
        if provider not in self.providers:
            raise ValueError(f"unknown provider: {provider}")
        return self.providers[provider].search(valid_query, **kwargs)

    def _run_many(self, names: list[str], query: str, options: dict[str, dict[str, Any]], limit: int) -> list[ProviderCall]:
        configured = [name for name in names if name in self.providers and self.providers[name].configured]
        if not configured:
            return []
        calls: list[ProviderCall] = []
        workers = min(max(1, self.settings.max_workers), len(configured))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.providers[name].search, query, limit=limit, **options.get(name, {})): name
                for name in configured
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    calls.append(future.result())
                except Exception as exc:  # defensive provider boundary
                    calls.append(
                        ProviderCall(
                            provider=name,
                            query=redact_sensitive_text(query),
                            status="error",
                            error=redact_sensitive_text(str(exc)),
                        )
                    )
        order = {name: index for index, name in enumerate(configured)}
        calls.sort(key=lambda call: order.get(call.provider, 999))
        return calls

    def search(
        self,
        query: str,
        *,
        mode: str = "auto",
        intensity: str = "adaptive",
        limit: int | None = None,
        freshness: str | None = None,
        domains: list[str] | None = None,
        all_fallbacks: bool = False,
        max_provider_calls: int | None = None,
        max_stages: int | None = None,
    ) -> SearchResponse:
        start = time.perf_counter()
        valid_query = _validate_query(query)
        normalized_intensity = _normalize_intensity(intensity)
        provider_call_budget = _positive_budget(max_provider_calls, "max_provider_calls")
        stage_budget = _positive_budget(max_stages, "max_stages")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise ValueError("limit must be a positive integer")
        result_limit = self.settings.result_limit if limit is None else limit
        if isinstance(result_limit, bool) or not isinstance(result_limit, int) or result_limit <= 0:
            raise ValueError("result_limit must be a positive integer")
        plan = self.router.plan(valid_query, mode, freshness=freshness, domains=domains)
        resolved_call_budget, resolved_stage_budget = _resolved_search_budget(
            plan,
            normalized_intensity,
            max_provider_calls=provider_call_budget,
            max_stages=stage_budget,
            all_fallbacks=all_fallbacks,
        )
        configured = set(self.configured_providers())
        planned_stages = _execution_stages(plan, normalized_intensity, configured)
        weights = self.router.weights_for(plan.mode)
        calls: list[ProviderCall] = []
        min_results = min(plan.min_results, result_limit)
        min_domains = min(2, min_results)
        stage_records: list[dict[str, Any]] = []
        stop_reason = "route_exhausted"

        for stage_index, stage_names in enumerate(planned_stages, start=1):
            if stage_index > resolved_stage_budget:
                stop_reason = "max_stages_reached"
                break
            remaining_calls = resolved_call_budget - len(calls)
            if remaining_calls <= 0:
                stop_reason = "max_provider_calls_reached"
                break
            selected_names = stage_names[:remaining_calls]
            stage_calls = self._run_many(selected_names, valid_query, plan.provider_options, result_limit)
            calls.extend(stage_calls)
            progress, distinct_domains = _fused_progress(
                calls,
                provider_weights=weights,
                rrf_k=self.settings.rrf_k,
                limit=result_limit,
                domain_cap=self.settings.domain_cap,
            )
            successful = [call.provider for call in stage_calls if call.ok]
            failed = [call.provider for call in stage_calls if not call.ok]
            gaps: list[str] = []
            if not any(call.ok for call in calls):
                gaps.append("no successful provider call")
            if normalized_intensity == "quick":
                sufficient = bool(progress)
                if not sufficient:
                    gaps.append("no usable result")
            else:
                if len(progress) < min_results:
                    gaps.append(f"usable results {len(progress)} < {min_results}")
                if distinct_domains < min_domains:
                    gaps.append(f"distinct domains {distinct_domains} < {min_domains}")
                sufficient = not gaps

            force_continue = normalized_intensity == "deep" or all_fallbacks
            has_more_allowed_stage = (
                stage_index < len(planned_stages)
                and stage_index < resolved_stage_budget
                and len(calls) < resolved_call_budget
            )
            stage_records.append(
                {
                    "stage": stage_index,
                    "providers": [call.provider for call in stage_calls],
                    "successful_providers": successful,
                    "failed_providers": failed,
                    "cumulative_usable_results": len(progress),
                    "cumulative_distinct_domains": distinct_domains,
                    "evidence_gaps": gaps,
                    "decision": "continue" if has_more_allowed_stage and (force_continue or not sufficient) else "stop",
                }
            )
            if sufficient and not force_continue:
                stop_reason = "quick_result_found" if normalized_intensity == "quick" else "evidence_target_met"
                break
            if len(calls) >= resolved_call_budget:
                stop_reason = (
                    "intensity_complete"
                    if stage_index >= len(planned_stages) and force_continue
                    else "max_provider_calls_reached"
                )
                break
            if stage_index >= resolved_stage_budget:
                stop_reason = "max_stages_reached"
                break
        else:
            if not planned_stages:
                stop_reason = "no_configured_providers"
            elif normalized_intensity == "deep" or all_fallbacks:
                stop_reason = "intensity_complete"

        warnings: list[str] = []
        missing_primary = [name for name in plan.primary if name not in configured]
        if missing_primary:
            warnings.append("unconfigured primary providers: " + ", ".join(missing_primary))
        failed = [call.provider for call in calls if not call.ok]
        if failed:
            warnings.append("provider errors: " + ", ".join(failed))

        fused = fuse_calls(
            deepcopy(calls),
            provider_weights=weights,
            rrf_k=self.settings.rrf_k,
            limit=result_limit,
            domain_cap=self.settings.domain_cap,
        )
        annotate_evidence(
            fused,
            [call.provider for call in calls if call.ok],
            reference_time=datetime.now(UTC),
        )
        return SearchResponse(
            query=valid_query,
            mode=plan.mode,
            plan=plan,
            results=fused,
            calls=calls,
            elapsed_ms=(time.perf_counter() - start) * 1000,
            warnings=warnings,
            execution={
                "intensity": normalized_intensity,
                "max_provider_calls": resolved_call_budget,
                "max_stages": resolved_stage_budget,
                "provider_call_count": len(calls),
                "stage_count": len(stage_records),
                "target_usable_results": min_results,
                "target_distinct_domains": min_domains,
                "stages": stage_records,
                "stop_reason": stop_reason,
            },
        )

    def fetch(self, url: str, provider: str = "auto") -> dict[str, Any]:
        valid_url = _validate_url(url)
        candidates = [provider] if provider != "auto" else list(_FETCH_CANDIDATES)
        errors: list[dict[str, str]] = []
        for name in candidates:
            adapter = self.providers.get(name)
            if adapter is None or not adapter.configured:
                continue
            try:
                if name == "firecrawl":
                    payload = adapter.scrape(valid_url)  # type: ignore[attr-defined]
                elif name == "tavily":
                    payload = adapter.extract([valid_url])  # type: ignore[attr-defined]
                elif name == "exa":
                    payload = adapter.contents([valid_url])  # type: ignore[attr-defined]
                else:
                    continue
            except Exception as exc:
                errors.append({"provider": name, "error": redact_sensitive_text(str(exc))})
                continue
            failure = _fetch_payload_failure(payload)
            if failure is None:
                safe_payload = sanitize_sensitive_value(payload, drop_sensitive_fields=True)
                return (
                    safe_payload
                    if isinstance(safe_payload, dict)
                    else {"ok": False, "url": redact_sensitive_text(valid_url)}
                )
            errors.append({"provider": name, "error": redact_sensitive_text(failure)})
            if provider != "auto" and isinstance(payload, Mapping):
                safe_payload = sanitize_sensitive_value(dict(payload), drop_sensitive_fields=True)
                return (
                    safe_payload
                    if isinstance(safe_payload, dict)
                    else {"ok": False, "url": redact_sensitive_text(valid_url)}
                )
        return sanitize_sensitive_value(
            {"ok": False, "url": valid_url, "errors": errors, "error": "no fetch provider succeeded"},
            drop_sensitive_fields=True,
        )

    def fetch_detailed(self, url: str, provider: str = "auto") -> FetchOutcome:
        valid_url = _validate_url(url)
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        candidates = [provider] if provider != "auto" else list(_FETCH_CANDIDATES)
        attempts: list[FetchAttempt] = []
        for name in candidates:
            adapter = self.providers.get(name)
            if adapter is None:
                attempts.append(FetchAttempt(provider=name, status="unsupported"))
                continue
            if not adapter.configured:
                attempts.append(FetchAttempt(provider=name, status="unconfigured"))
                continue
            method_name = _FETCH_METHODS.get(name)
            method = getattr(adapter, method_name, None) if method_name else None
            if not callable(method):
                attempts.append(FetchAttempt(provider=name, status="unsupported"))
                continue
            start = time.perf_counter()
            try:
                payload = method(valid_url) if name == "firecrawl" else method([valid_url])
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                http_status = getattr(exc, "status", None)
                attempts.append(
                    FetchAttempt(
                        provider=name,
                        status="error",
                        elapsed_ms=elapsed_ms,
                        http_status=http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else None,
                        error=str(exc),
                    )
                )
                continue
            elapsed_ms = _payload_elapsed_ms(payload, (time.perf_counter() - start) * 1000)
            http_status = _payload_http_status(payload)
            failure = _fetch_payload_failure(payload)
            if failure is not None:
                attempts.append(
                    FetchAttempt(
                        provider=name,
                        status="error",
                        elapsed_ms=elapsed_ms,
                        http_status=http_status,
                        error=failure,
                    )
                )
                continue
            content = _normalized_content(payload)
            attempts.append(FetchAttempt(provider=name, status="ok", elapsed_ms=elapsed_ms, http_status=http_status))
            return FetchOutcome(
                url=valid_url,
                attempts=attempts,
                selected_provider=name,
                content=content,
                payload=payload,
            )
        return FetchOutcome(url=valid_url, attempts=attempts)

    def multi_search(
        self,
        queries: Sequence[str],
        *,
        mode: str = "auto",
        intensity: str = "adaptive",
        limit: int | None = None,
        freshness: str | None = None,
        domains: list[str] | None = None,
        all_fallbacks: bool = False,
        max_provider_calls: int | None = None,
        max_stages: int | None = None,
    ) -> list[SearchResponse]:
        if isinstance(queries, (str, bytes, bytearray)) or not isinstance(queries, Sequence) or not queries:
            raise ValueError("queries must be a non-empty sequence of non-empty strings")
        ordered_queries = [_validate_query(query) for query in queries]
        return [
            self.search(
                query,
                mode=mode,
                intensity=intensity,
                limit=limit,
                freshness=freshness,
                domains=domains,
                all_fallbacks=all_fallbacks,
                max_provider_calls=max_provider_calls,
                max_stages=max_stages,
            )
            for query in ordered_queries
        ]

    def research_plan(
        self,
        query: str,
        *,
        mode: str = "auto",
        intensity: str = "adaptive",
        freshness: str | None = None,
        domains: list[str] | None = None,
        max_provider_calls: int | None = None,
        max_stages: int | None = None,
    ) -> dict[str, Any]:
        valid_query = _validate_query(query)
        plan = self.router.plan(valid_query, mode, freshness=freshness, domains=domains)
        return build_research_plan(
            valid_query,
            plan,
            intensity=intensity,
            max_provider_calls=max_provider_calls,
            max_stages=max_stages,
        )

    def collect_evidence(
        self,
        query: str,
        *,
        urls: Sequence[str] | None = None,
        fetch_limit: int = 3,
        mode: str = "auto",
        intensity: str = "adaptive",
        limit: int | None = None,
        freshness: str | None = None,
        domains: list[str] | None = None,
        all_fallbacks: bool = False,
        max_provider_calls: int | None = None,
        max_stages: int | None = None,
    ) -> dict[str, Any]:
        valid_query = _validate_query(query)
        if isinstance(fetch_limit, bool) or not isinstance(fetch_limit, int) or fetch_limit <= 0:
            raise ValueError("fetch_limit must be a positive integer")
        if urls is not None and (isinstance(urls, (str, bytes, bytearray)) or not isinstance(urls, Sequence)):
            raise ValueError("urls must be a sequence of non-empty strings")
        explicit_urls: list[tuple[str, str]] = []
        explicit_canonical_urls: set[str] = set()
        if urls:
            for value in urls:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("urls must be a sequence of non-empty strings")
                normalized = canonical_url(value)
                if normalized and normalized not in explicit_canonical_urls:
                    explicit_canonical_urls.add(normalized)
                    explicit_urls.append((value, normalized))

        response = self.search(
            valid_query,
            mode=mode,
            intensity=intensity,
            limit=limit,
            freshness=freshness,
            domains=domains,
            all_fallbacks=all_fallbacks,
            max_provider_calls=max_provider_calls,
            max_stages=max_stages,
        )
        result_by_url: dict[str, Any] = {}
        ranked_urls: list[tuple[str, str]] = []
        for result in response.results:
            normalized = canonical_url(result.url)
            if normalized and normalized not in result_by_url:
                result_by_url[normalized] = result
                ranked_urls.append((result.url, normalized))
        selected_urls = (explicit_urls if explicit_urls else ranked_urls)[:fetch_limit]
        evidence: list[dict[str, Any]] = []
        for selected_url, normalized_url in selected_urls:
            outcome = self.fetch_detailed(selected_url)
            result = result_by_url.get(normalized_url)
            evidence.append(
                {
                    "url": selected_url,
                    "canonical_url": normalized_url,
                    "result": result.to_dict() if result is not None else None,
                    "fetch": outcome.to_dict(),
                }
            )
        attempt_count = sum(len(item["fetch"]["attempts"]) for item in evidence)
        successful_fetches = sum(1 for item in evidence if item["fetch"].get("ok") is True)
        return sanitize_sensitive_value({
            "query": valid_query,
            "verification_status": "not_verified",
            "search": response.to_dict(),
            "evidence": evidence,
            "counts": {
                "selected_urls": len(selected_urls),
                "fetched_urls": len(evidence),
                "successful_fetches": successful_fetches,
                "fetch_attempts": attempt_count,
            },
        })
