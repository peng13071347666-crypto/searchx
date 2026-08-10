from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path

from searchx.benchmark import (
    BenchmarkCase,
    BenchmarkRunner,
    compact_report,
    load_cases,
    tune_profile,
    validate_tunable_report,
)
from searchx.models import ProviderCall, SearchResult


class BenchmarkDataTests(unittest.TestCase):
    def test_default_dataset_has_balanced_valid_cases(self) -> None:
        cases = load_cases()

        self.assertEqual(len(cases), 72)
        self.assertEqual(
            Counter(case.scenario for case in cases),
            {
                "quick": 8,
                "web": 8,
                "official": 8,
                "fresh": 8,
                "news": 8,
                "code": 8,
                "academic": 8,
                "cn": 8,
                "deep": 8,
            },
        )
        self.assertEqual(len({case.id for case in cases}), 72)
        self.assertGreaterEqual(sum(any(ord(character) > 127 for character in case.query) for case in cases), 18)
        self.assertTrue(all(case.freshness for case in cases if case.scenario in {"fresh", "news"}))
        self.assertTrue(all(case.freshness is None for case in cases if case.scenario not in {"fresh", "news"}))
        self.assertTrue(all("2026" not in case.query and "August" not in case.query for case in cases))

    def test_load_cases_rejects_invalid_schema_and_duplicates(self) -> None:
        valid = {"id": "valid", "scenario": "quick", "query": "safe query"}
        invalid_cases: list[tuple[object, str]] = [
            ({"id": "not-a-list"}, "list"),
            (["not-an-object"], "object"),
            ([{"id": "", "scenario": "quick", "query": "query"}], "id"),
            ([valid, {"id": "valid", "scenario": "web", "query": "another query"}], "duplicate"),
            ([{"id": "unsupported", "scenario": "other", "query": "query"}], "unsupported"),
            ([{"id": "bad-list", "scenario": "quick", "query": "query", "expected_terms": "term"}], "expected_terms"),
            ([{"id": "bad-item", "scenario": "quick", "query": "query", "providers": [""]}], "providers"),
            ([{"id": "bad-freshness", "scenario": "quick", "query": "query", "freshness": "hour"}], "freshness"),
            ([{"id": "bad-domain", "scenario": "quick", "query": "query", "expected_domains": ["https://example.test"]}], "expected_domains"),
            ([{"id": "path-domain", "scenario": "quick", "query": "query", "expected_domains": ["example.test/path"]}], "expected_domains"),
            ([{"id": "space-domain", "scenario": "quick", "query": "query", "expected_domains": [" example.test"]}], "expected_domains"),
            ([{"id": "bad-provider", "scenario": "quick", "query": "query", "providers": ["unknown"]}], "unsupported provider"),
            ([{"id": "fresh-without-window", "scenario": "fresh", "query": "query"}], "freshness is required"),
            ([{"id": "news-without-window", "scenario": "news", "query": "query"}], "freshness is required"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            for data, message in invalid_cases:
                with self.subTest(message=message):
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_cases(path)

            path.write_text(
                json.dumps(
                    [{"id": "deduplicated", "scenario": "quick", "query": "query", "providers": ["serper", "serper", "brave"]}]
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_cases(path)[0].providers, ["serper", "brave"])


class BenchmarkRunnerTests(unittest.TestCase):
    def test_quick_official_scheduling_and_report_transparency(self) -> None:
        class FakeEngine:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, object]]] = []

            def configured_providers(self) -> list[str]:
                return ["exa", "serper"]

            def provider_search(self, provider: str, query: str, **options: object) -> ProviderCall:
                self.calls.append((provider, query, dict(options)))
                return ProviderCall(provider=provider, query=query)

        cases = [
            BenchmarkCase(id="quick-case", scenario="quick", query="quick query"),
            BenchmarkCase(id="official-case", scenario="official", query="official query"),
        ]
        engine = FakeEngine()
        runner = BenchmarkRunner(engine)  # type: ignore[arg-type]

        report = runner.run(cases, workers=1)

        self.assertEqual(report["selected_case_ids"], ["quick-case", "official-case"])
        self.assertEqual(report["configured_providers"], ["exa", "serper"])
        self.assertNotIn("requested_provider_filter", report)
        self.assertEqual(report["scheduled_call_count"], 3)
        self.assertEqual(report["skipped_unconfigured_call_count"], 4)
        self.assertEqual(report["call_count"], 3)
        self.assertEqual({row["request_options"]["mode"] for row in report["rows"]}, {"quick", "official"})
        self.assertTrue(all(row["request_options"]["limit"] == 10 for row in report["rows"]))
        compact = compact_report(report)
        self.assertEqual(compact["selected_case_ids"], ["quick-case", "official-case"])

        filtered = runner.run(cases, providers={"exa"}, workers=1)
        self.assertEqual(filtered["requested_provider_filter"], ["exa"])
        self.assertEqual(filtered["scheduled_call_count"], 1)
        self.assertEqual(filtered["skipped_unconfigured_call_count"], 0)
        self.assertEqual(filtered["rows"][0]["provider"], "exa")

    def test_workers_must_be_positive(self) -> None:
        class EmptyEngine:
            def configured_providers(self) -> list[str]:
                return []

        runner = BenchmarkRunner(EmptyEngine())  # type: ignore[arg-type]
        for workers in (0, -1, True):
            with self.subTest(workers=workers):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    runner.run([], workers=workers)  # type: ignore[arg-type]

    def test_runner_rejects_unknown_filters_and_single_provider_uniqueness_is_unavailable(self) -> None:
        class SingleProviderEngine:
            def configured_providers(self) -> list[str]:
                return ["serper"]

            def provider_search(self, provider: str, query: str, **options: object) -> ProviderCall:
                return ProviderCall(
                    provider=provider,
                    query=query,
                    results=[SearchResult("only", "https://example.test/only", provider=provider, rank=1)],
                )

        case = BenchmarkCase(id="single", scenario="quick", query="query", providers=["serper"])
        runner = BenchmarkRunner(SingleProviderEngine())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "unknown benchmark scenario"):
            runner.run([case], scenarios={"unknown"}, workers=1)
        with self.assertRaisesRegex(ValueError, "unknown benchmark provider"):
            runner.run([case], providers={"unknown"}, workers=1)
        with self.assertRaisesRegex(ValueError, "hashable"):
            runner.run([case], scenarios=[{"not": "hashable"}], workers=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "freshness is required"):
            runner.run([BenchmarkCase(id="fresh", scenario="fresh", query="query")], workers=1)

        report = runner.run([case], workers=1)
        row = report["rows"][0]
        self.assertEqual(row["comparison_provider_count"], 1)
        self.assertIsNone(row["unique_ratio"])
        self.assertIsNone(report["summary"]["quick"]["serper"]["unique_ratio"])

        class TwoProviderEngine:
            def configured_providers(self) -> list[str]:
                return ["serper", "brave"]

            def provider_search(self, provider: str, query: str, **options: object) -> ProviderCall:
                return ProviderCall(
                    provider=provider,
                    query=query,
                    results=[SearchResult(provider, f"https://{provider}.example.test/result", provider=provider, rank=1)],
                )

        comparable = BenchmarkRunner(TwoProviderEngine()).run(  # type: ignore[arg-type]
            [BenchmarkCase(id="two", scenario="quick", query="query", providers=["serper", "brave"])],
            workers=1,
        )
        self.assertTrue(all(row["comparison_provider_count"] == 2 for row in comparable["rows"]))
        self.assertTrue(all(row["unique_ratio"] == 1.0 for row in comparable["rows"]))

        class ZeroResultComparatorEngine:
            def configured_providers(self) -> list[str]:
                return ["serper", "brave"]

            def provider_search(self, provider: str, query: str, **options: object) -> ProviderCall:
                results = []
                if provider == "serper":
                    results = [SearchResult("only", "https://serper.example.test/result", provider=provider, rank=1)]
                return ProviderCall(provider=provider, query=query, results=results)

        zero_result_comparator = BenchmarkRunner(ZeroResultComparatorEngine()).run(  # type: ignore[arg-type]
            [BenchmarkCase(id="zero", scenario="quick", query="query", providers=["serper", "brave"])],
            workers=1,
        )
        self.assertTrue(all(row["comparison_provider_count"] == 1 for row in zero_result_comparator["rows"]))
        self.assertTrue(all(row["unique_ratio"] is None for row in zero_result_comparator["rows"]))

    def test_freshness_metrics_capabilities_and_tuning_exclusion(self) -> None:
        now = datetime.now(UTC)
        fresh = (now - timedelta(hours=2)).isoformat()
        stale_rfc = (now - timedelta(days=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")

        class FreshnessEngine:
            def configured_providers(self) -> list[str]:
                return ["serper", "exa"]

            def provider_search(self, provider: str, query: str, **options: object) -> ProviderCall:
                if options["freshness"] != "day":
                    raise AssertionError("freshness option was not forwarded")
                return ProviderCall(
                    provider=provider,
                    query=query,
                    results=[
                        SearchResult("fresh item", "https://fresh.example/new", published_at=fresh, provider=provider),
                        SearchResult("stale item", "https://fresh.example/old", published_at=stale_rfc, provider=provider),
                        SearchResult("undated item", "https://fresh.example/unknown", published_at="not a timestamp", provider=provider),
                    ],
                )

        case = BenchmarkCase(
            id="freshness",
            scenario="fresh",
            query="fresh item",
            expected_domains=["fresh.example"],
            expected_terms=["fresh"],
            freshness="day",
            providers=["serper", "exa"],
        )
        report = BenchmarkRunner(FreshnessEngine()).run([case], workers=1)  # type: ignore[arg-type]
        rows = {row["provider"]: row for row in report["rows"]}

        for row in rows.values():
            self.assertEqual(row["expected_domains"], ["fresh.example"])
            self.assertEqual(row["expected_terms"], ["fresh"])
            self.assertEqual(row["freshness"], "day")
            self.assertAlmostEqual(row["dated_result_coverage"], 2 / 3, places=4)
            self.assertAlmostEqual(row["stale_ratio"], 0.5, places=4)
            self.assertAlmostEqual(row["freshness_score"], 1 / 3, places=4)
            self.assertAlmostEqual(row["quality"]["metadata"], 2 / 3, places=4)
            self.assertGreater(row["quality"]["freshness_score"], 0.0)
        self.assertTrue(rows["serper"]["freshness_filter_supported"])
        self.assertFalse(rows["exa"]["freshness_filter_supported"])
        self.assertTrue(report["summary"]["fresh"]["serper"]["freshness_filter_supported"])
        self.assertFalse(report["summary"]["fresh"]["exa"]["freshness_filter_supported"])
        self.assertIn("serper", tune_profile(report)["routes"]["fresh"]["primary"])
        self.assertNotIn("exa", tune_profile(report)["routes"]["fresh"]["primary"])
        self.assertNotIn(
            "fresh",
            tune_profile({"summary": {"fresh": {"exa": report["summary"]["fresh"]["exa"]}}})["routes"],
        )

    def test_tuning_uses_only_known_reliable_candidates_and_static_freshness_capability(self) -> None:
        def metrics(
            success_rate: float,
            quality: float,
            latency_ms: float = 100.0,
            unique_ratio: float | None = 0.1,
            avg_results: float = 1.0,
            **extra: object,
        ) -> dict[str, object]:
            values: dict[str, object] = {
                "success_rate": success_rate,
                "quality": quality,
                "latency_ms": latency_ms,
                "avg_results": avg_results,
            }
            if unique_ratio is not None:
                values["unique_ratio"] = unique_ratio
            values.update(extra)
            return values

        web_report = {
            "summary": {
                "web": {
                    # This deliberately scores well but cannot be tuned because
                    # it is below the reliable-success threshold.
                    "serper": metrics(0.49, 1.0, 1.0, 1.0),
                    "brave": metrics(1.0, 0.9),
                    "tavily": metrics(1.0, 0.8),
                    "exa": metrics(1.0, 0.7),
                    "firecrawl": metrics(1.0, 0.6),
                    "github": metrics(1.0, float("nan")),
                    "unknown": metrics(1.0, 1.0),
                }
            }
        }
        web_profile = tune_profile(web_report)
        self.assertEqual(web_profile["routes"]["web"]["primary"], ["brave", "tavily", "exa"])
        self.assertEqual(web_profile["routes"]["web"]["fallback"], ["firecrawl"])
        self.assertEqual(set(web_profile["mode_provider_weights"]["web"]), {"brave", "tavily", "exa", "firecrawl"})

        fresh_report = {
            "summary": {
                "fresh": {
                    "serper": metrics(1.0, 0.8, freshness_filter_supported=True),
                    # A forged report claim cannot override known adapter behavior.
                    "exa": metrics(1.0, 1.0, freshness_filter_supported=True),
                    "firecrawl": metrics(1.0, 1.0, freshness_filter_supported=True),
                    "github": metrics(1.0, 1.0, freshness_filter_supported=True),
                }
            }
        }
        fresh_profile = tune_profile(fresh_report)
        self.assertEqual(fresh_profile["routes"]["fresh"]["primary"], ["serper"])
        self.assertEqual(set(fresh_profile["mode_provider_weights"]["fresh"]), {"serper"})

        zero_result_report = {
            "summary": {
                "web": {
                    "serper": metrics(1.0, 1.0, avg_results=0.0),
                    "brave": metrics(0.5, 0.2, avg_results=1.0),
                }
            }
        }
        zero_result_profile = tune_profile(zero_result_report)
        self.assertEqual(zero_result_profile["routes"]["web"]["primary"], ["brave"])
        self.assertEqual(set(zero_result_profile["mode_provider_weights"]["web"]), {"brave"})

    def test_tuning_skips_invalid_summaries_and_validation_rejects_them(self) -> None:
        valid_metrics = {"success_rate": 1.0, "quality": 0.8, "latency_ms": 10.0, "avg_results": 1.0}
        mixed_summary = {
            "summary": {
                "unknown-scenario": {"serper": valid_metrics},
                "web": {
                    "unknown-provider": valid_metrics,
                    "serper": valid_metrics,
                    "brave": {"success_rate": 1.0, "quality": 1.1, "latency_ms": 10.0, "avg_results": 1.0},
                },
            }
        }
        profile = tune_profile(mixed_summary)
        self.assertEqual(profile["routes"]["web"]["primary"], ["serper"])
        self.assertEqual(set(profile["mode_provider_weights"]["web"]), {"serper"})

        invalid_report = {
            "call_count": 1,
            "rows": [{"status": "ok"}],
            **mixed_summary,
        }
        with self.assertRaisesRegex(ValueError, "unknown scenario"):
            validate_tunable_report(invalid_report)

        bad_metrics_report = {
            "call_count": 1,
            "rows": [{"status": "ok"}],
            "summary": {
                "web": {"serper": {"success_rate": 1.0, "quality": float("nan"), "latency_ms": 10.0, "avg_results": 1.0}}
            },
        }
        self.assertEqual(tune_profile(bad_metrics_report)["routes"], {})
        with self.assertRaisesRegex(ValueError, "invalid provider metrics"):
            validate_tunable_report(bad_metrics_report)

        for avg_results in (0.0, None, float("nan"), "one"):
            with self.subTest(avg_results=avg_results):
                report = {
                    "call_count": 2,
                    "rows": [{"status": "ok"}, {"status": "ok"}],
                    "summary": {
                        "web": {
                            "serper": {
                                "success_rate": 1.0,
                                "quality": 0.8,
                                "latency_ms": 10.0,
                                "avg_results": avg_results,
                            },
                            "brave": {"success_rate": 0.5, "quality": 0.2, "latency_ms": 10.0, "avg_results": 1.0},
                        }
                    }
                }
                validate_tunable_report(report)
                profile = tune_profile(report)
                self.assertEqual(profile["routes"]["web"]["primary"], ["brave"])

        missing_avg_results_report = {
            "call_count": 1,
            "rows": [{"status": "ok"}],
            "summary": {"web": {"serper": {"success_rate": 1.0, "quality": 0.8, "latency_ms": 10.0}}},
        }
        self.assertEqual(tune_profile(missing_avg_results_report)["routes"], {})
        with self.assertRaisesRegex(ValueError, "no usable routes"):
            validate_tunable_report(missing_avg_results_report)


if __name__ == "__main__":
    unittest.main()
