from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .benchmark import (
    SUPPORTED_BENCHMARK_PROVIDERS,
    SUPPORTED_SCENARIOS,
    BenchmarkRunner,
    compact_report,
    load_cases,
    tune_profile,
    validate_tunable_report,
)
from .config import ENV_KEYS, Settings, credential_status, load_local_secrets
from .engine import SEARCH_INTENSITIES, SearchEngine, build_research_plan
from .models import redact_sensitive_text, sanitize_sensitive_value
from .router import Router
from .secrets import configure_secrets, secrets_metadata


def _known_secret_values() -> list[str]:
    """Return configured values only for in-memory redaction, never output."""
    values = {os.environ.get(name, "") for name in ENV_KEYS.values()}
    return sorted((value for value in values if value), key=len, reverse=True)


def _redact_text(value: str) -> str:
    text = value
    for secret in _known_secret_values():
        text = text.replace(secret, "[redacted]")
    return redact_sensitive_text(text)


def _redact(value: Any) -> Any:
    """Recursively remove credentials from values headed to stdout, stderr, or disk."""
    return _redact_known_values(sanitize_sensitive_value(value))


def _redact_known_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_redact_text(str(key)): _redact_known_values(item) for key, item in value.items()}
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact_known_values(item) for item in value]
    return value


def _json_text(data: Any) -> str:
    return json.dumps(
        _redact(data),
        ensure_ascii=False,
        indent=2,
        default=lambda value: _redact_text(str(value)),
    )


def _emit(data: Any, *, stream: Any | None = None) -> None:
    target = sys.stdout if stream is None else stream
    target.write(_json_text(data))
    target.write("\n")


def _emit_error(message: str) -> None:
    _emit({"ok": False, "error": _redact_text(message)}, stream=sys.stderr)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Use argparse ergonomics while preventing accidental argument leakage."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {_redact_text(message)}\n")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _validated_query(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("query must be a non-empty string")
    return value


def _validated_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("url must be a non-empty string")
    return value


def _add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        metavar="PATH",
        help="optional route/weight profile JSON file",
    )


def _add_search_option_arguments(
    parser: argparse.ArgumentParser,
    *,
    direct_provider: bool = False,
) -> None:
    parser.add_argument("--limit", type=_positive_int, help="maximum results per provider")
    parser.add_argument("--mode", help="route mode (default: auto)")
    parser.add_argument(
        "--freshness",
        choices=("day", "week", "month", "year"),
        help="prefer recent results",
    )
    parser.add_argument(
        "--domain",
        dest="domains",
        action="append",
        metavar="DOMAIN",
        help="restrict to a domain; repeat to allow several",
    )
    _add_profile_argument(parser)
    if direct_provider:
        parser.add_argument("--category", help="provider-specific search category")
        parser.add_argument("--depth", help="provider-specific search depth")
        parser.add_argument(
            "--full-content",
            dest="full_content",
            action="store_true",
            default=None,
            help="request provider full content when supported",
        )


def _add_search_arguments(
    parser: argparse.ArgumentParser,
    *,
    direct_provider: bool = False,
) -> None:
    parser.add_argument("query", help="search query (quote multi-word queries in the shell)")
    _add_search_option_arguments(parser, direct_provider=direct_provider)


def _add_strategy_arguments(parser: argparse.ArgumentParser, *, per_query: bool = False) -> None:
    scope = " per query" if per_query else ""
    parser.add_argument(
        "--intensity",
        choices=SEARCH_INTENSITIES,
        default="adaptive",
        help="execution strength: quick, adaptive, or deep (default: adaptive)",
    )
    parser.add_argument(
        "--max-provider-calls",
        type=_positive_int,
        metavar="N",
        help=f"hard maximum provider calls{scope}",
    )
    parser.add_argument(
        "--max-stages",
        type=_positive_int,
        metavar="N",
        help=f"hard maximum progressive search stages{scope}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="searchx",
        description="Unified multi-provider web search.",
    )
    parser.add_argument("--version", action="version", version="searchx 0.2.0")
    commands = parser.add_subparsers(dest="command", required=True, title="commands")

    configure = commands.add_parser("configure", help="interactively save local provider credentials")
    configure.set_defaults(handler=_run_configure)

    doctor = commands.add_parser("doctor", help="show local configuration status without network access")
    doctor.set_defaults(handler=_run_doctor)

    provider = commands.add_parser("provider", help="search one provider directly")
    provider.add_argument("provider", help="provider name")
    _add_search_arguments(provider, direct_provider=True)
    provider.set_defaults(handler=_run_provider)

    search = commands.add_parser("search", help="search with automatic routing and fusion")
    _add_search_arguments(search)
    _add_strategy_arguments(search)
    search.set_defaults(mode="auto", handler=_run_search)
    search.add_argument(
        "--all-fallbacks",
        action="store_true",
        help="also run all fallback providers",
    )

    multi_search = commands.add_parser("multi-search", help="run several routed searches in input order")
    multi_search.add_argument("queries", nargs="+", metavar="QUERY", help="one or more search queries")
    _add_search_option_arguments(multi_search)
    _add_strategy_arguments(multi_search, per_query=True)
    multi_search.add_argument(
        "--all-fallbacks",
        action="store_true",
        help="also run all fallback providers for each query",
    )
    multi_search.set_defaults(mode="auto", handler=_run_multi_search)

    fetch = commands.add_parser("fetch", help="fetch page content through a configured extraction provider")
    fetch.add_argument("url", help="page URL")
    fetch.add_argument("--provider", default="auto", help="firecrawl, tavily, exa, or auto")
    _add_profile_argument(fetch)
    fetch.set_defaults(handler=_run_fetch)

    explain = commands.add_parser("explain-route", help="show the route chosen for a query without searching")
    explain.add_argument("query", help="search query")
    explain.add_argument("--mode", default="auto", help="route mode (default: auto)")
    explain.add_argument(
        "--freshness",
        choices=("day", "week", "month", "year"),
        help="prefer recent results",
    )
    explain.add_argument(
        "--domain",
        dest="domains",
        action="append",
        metavar="DOMAIN",
        help="restrict to a domain; repeat to allow several",
    )
    _add_profile_argument(explain)
    explain.set_defaults(handler=_run_explain_route)

    research = commands.add_parser("research-plan", help="show a bounded offline research workflow")
    research.add_argument("query", help="research query (quote multi-word queries in the shell)")
    research.add_argument("--mode", default="auto", help="route mode (default: auto)")
    research.add_argument(
        "--freshness",
        choices=("day", "week", "month", "year"),
        help="prefer recent results",
    )
    research.add_argument(
        "--domain",
        dest="domains",
        action="append",
        metavar="DOMAIN",
        help="restrict to a domain; repeat to allow several",
    )
    _add_strategy_arguments(research)
    _add_profile_argument(research)
    research.set_defaults(handler=_run_research_plan)

    evidence = commands.add_parser("evidence", help="collect non-verified search and fetch evidence")
    _add_search_arguments(evidence)
    _add_strategy_arguments(evidence)
    evidence.add_argument("--url", dest="urls", action="append", metavar="URL", help="explicit URL to fetch; repeatable")
    evidence.add_argument("--fetch-limit", type=_positive_int, default=3, help="maximum URLs to fetch (default: 3)")
    evidence.add_argument(
        "--all-fallbacks",
        action="store_true",
        help="also run all fallback providers before selecting URLs",
    )
    evidence.set_defaults(mode="auto", handler=_run_evidence)

    bench = commands.add_parser("bench", help="run configured providers against benchmark cases")
    bench.add_argument("--cases", metavar="PATH", help="benchmark case JSON file")
    bench.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=sorted(SUPPORTED_SCENARIOS),
        help="scenario to include; repeatable",
    )
    bench.add_argument(
        "--provider",
        dest="providers",
        action="append",
        choices=sorted(SUPPORTED_BENCHMARK_PROVIDERS),
        help="provider to include; repeatable",
    )
    bench.add_argument("--max-cases", type=_nonnegative_int, help="limit selected benchmark cases")
    bench.add_argument("--workers", type=int, default=4, help="parallel benchmark workers (default: 4)")
    bench.add_argument("--output", "-o", metavar="PATH", help="write the full benchmark report to PATH")
    bench.add_argument("--full", action="store_true", help="print full benchmark rows instead of a compact summary")
    _add_profile_argument(bench)
    bench.set_defaults(handler=_run_bench)

    tune = commands.add_parser("tune", help="generate a route profile from a benchmark report")
    tune.add_argument("report", nargs="?", metavar="REPORT", help="benchmark report JSON file")
    tune.add_argument("--input", dest="input_report", metavar="REPORT", help="benchmark report JSON file")
    tune.add_argument("--output", "-o", metavar="PATH", help="write generated profile JSON to PATH")
    tune.set_defaults(handler=_run_tune)

    return parser


def _engine(args: argparse.Namespace) -> SearchEngine:
    profile = getattr(args, "profile", None)
    return SearchEngine(settings=Settings.load(profile), profile_path=profile)


def _provided_options(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "limit": args.limit,
        "mode": args.mode,
        "freshness": args.freshness,
        "domains": args.domains,
        "category": getattr(args, "category", None),
        "depth": getattr(args, "depth", None),
        "full_content": getattr(args, "full_content", None),
    }
    return {name: value for name, value in values.items() if value is not None}


def _run_configure(_: argparse.Namespace) -> int:
    path = configure_secrets()
    _emit({"ok": True, "secrets_file": secrets_metadata(path)})
    return 0


def _run_doctor(_: argparse.Namespace) -> int:
    # This deliberately avoids SearchEngine construction and all provider calls.
    providers = {
        provider: {"configured": bool(status.get("configured"))}
        for provider, status in credential_status().items()
    }
    _emit({"ok": True, "providers": providers, "secrets_file": secrets_metadata()})
    return 0


def _run_provider(args: argparse.Namespace) -> int:
    query = _validated_query(args.query)
    call = _engine(args).provider_search(args.provider, query, **_provided_options(args))
    _emit(call.to_dict(include_results=True))
    return 0 if call.ok else 1


def _run_search(args: argparse.Namespace) -> int:
    query = _validated_query(args.query)
    response = _engine(args).search(
        query,
        mode=args.mode,
        intensity=args.intensity,
        limit=args.limit,
        freshness=args.freshness,
        domains=args.domains,
        all_fallbacks=args.all_fallbacks,
        max_provider_calls=args.max_provider_calls,
        max_stages=args.max_stages,
    )
    _emit(response.to_dict())
    return 0 if any(call.ok for call in response.calls) else 1


def _run_multi_search(args: argparse.Namespace) -> int:
    queries = [_validated_query(query) for query in args.queries]
    responses = _engine(args).multi_search(
        queries,
        mode=args.mode,
        intensity=args.intensity,
        limit=args.limit,
        freshness=args.freshness,
        domains=args.domains,
        all_fallbacks=args.all_fallbacks,
        max_provider_calls=args.max_provider_calls,
        max_stages=args.max_stages,
    )
    items = [
        {
            "query": response.query,
            "ok": any(call.ok for call in response.calls),
            "response": response.to_dict(),
        }
        for response in responses
    ]
    any_ok = any(item["ok"] for item in items)
    _emit({"ok": any_ok, "responses": items})
    return 0 if any_ok else 1


def _run_fetch(args: argparse.Namespace) -> int:
    url = _validated_url(args.url)
    result = _engine(args).fetch(url, provider=args.provider)
    _emit(result)
    return 1 if isinstance(result, Mapping) and result.get("ok") is False else 0


def _run_explain_route(args: argparse.Namespace) -> int:
    query = _validated_query(args.query)
    plan = Router(Settings.load(args.profile), args.profile).plan(
        query,
        args.mode,
        freshness=args.freshness,
        domains=args.domains,
    )
    _emit({"query": query, "plan": plan.to_dict()})
    return 0


def _run_research_plan(args: argparse.Namespace) -> int:
    query = _validated_query(args.query)
    router = Router(Settings.load(args.profile), args.profile)
    plan = router.plan(query, args.mode, freshness=args.freshness, domains=args.domains)
    _emit(
        build_research_plan(
            query,
            plan,
            intensity=args.intensity,
            max_provider_calls=args.max_provider_calls,
            max_stages=args.max_stages,
        )
    )
    return 0


def _run_evidence(args: argparse.Namespace) -> int:
    query = _validated_query(args.query)
    urls = [_validated_url(url) for url in args.urls] if args.urls else None
    envelope = _engine(args).collect_evidence(
        query,
        urls=urls,
        fetch_limit=args.fetch_limit,
        mode=args.mode,
        intensity=args.intensity,
        limit=args.limit,
        freshness=args.freshness,
        domains=args.domains,
        all_fallbacks=args.all_fallbacks,
        max_provider_calls=args.max_provider_calls,
        max_stages=args.max_stages,
    )
    _emit(envelope)
    evidence = envelope.get("evidence", []) if isinstance(envelope, Mapping) else []
    return (
        0
        if any(
            isinstance(item, Mapping)
            and isinstance(item.get("fetch"), Mapping)
            and item["fetch"].get("ok") is True
            for item in evidence
        )
        else 1
    )


def _write_json(path_value: str, data: Any) -> None:
    path = Path(path_value).expanduser()
    payload = _json_text(data) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _run_bench(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(args.cases)
    except (OSError, TypeError, ValueError):
        raise ValueError("unable to load benchmark cases") from None
    report = BenchmarkRunner(_engine(args)).run(
        cases,
        scenarios=set(args.scenarios) if args.scenarios else None,
        providers=set(args.providers) if args.providers else None,
        max_cases=args.max_cases,
        workers=args.workers,
    )
    if args.output:
        _write_json(args.output, report)
    output = report if args.full else compact_report(report)
    if args.output:
        output = {**output, "report_written": True}
    _emit(output)
    successful_calls = sum(1 for row in report["rows"] if row.get("status") == "ok")
    return 0 if report["scheduled_call_count"] > 0 and successful_calls > 0 else 1


def _load_report(path_value: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path_value).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("unable to read benchmark report") from None
    if not isinstance(value, dict):
        raise ValueError("benchmark report must be a JSON object")
    return value


def _run_tune(args: argparse.Namespace) -> int:
    report_path = args.input_report or args.report
    if not report_path:
        raise ValueError("a benchmark report is required")
    report = _load_report(report_path)
    validate_tunable_report(report)
    profile = tune_profile(report)
    if not profile["routes"]:
        raise ValueError("benchmark report generated no usable routes")
    if args.output:
        _write_json(args.output, profile)
    output: dict[str, Any] = profile
    if args.output:
        output = {**profile, "profile_written": True}
    _emit(output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the SearchX CLI and return a shell-style exit status."""
    # The loader itself treats absent and malformed files as a no-op.  Loading
    # before parsing also makes every command—including ``doctor``—consistent.
    load_local_secrets()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        _emit_error("cancelled")
        return 130
    except Exception as exc:
        _emit_error(str(exc) or "command failed")
        return 1
