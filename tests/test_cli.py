from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from searchx import cli
from searchx import secrets
from searchx.benchmark import BenchmarkCase, BenchmarkRunner
from searchx.config import Settings
from searchx.engine import SearchEngine, build_research_plan
from searchx.models import ProviderCall, SearchPlan, SearchResponse, SearchResult
from searchx.router import Router
from searchx.secrets import load_secrets


class _RecordingEnvironment(dict[str, str]):
    def __init__(self, *args: object, **kwargs: str) -> None:
        super().__init__(*args, **kwargs)
        self.writes: list[str] = []

    def __setitem__(self, key: str, value: str) -> None:
        self.writes.append(key)
        super().__setitem__(key, value)


class SearchXCliTests(unittest.TestCase):
    def _run_cli(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_dotenv_loading_keeps_explicit_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.env"
            path.write_text(
                "SERPER_API_KEY=file-value\n"
                "BRAVE_API_KEY='another-file-value'\n"
                "UNRELATED_VALUE=ignored\n"
                "this is malformed\n",
                encoding="utf-8",
            )
            environment = _RecordingEnvironment({"SERPER_API_KEY": "process-value"})

            loaded = load_secrets(path, environment)

        self.assertEqual(loaded, {"BRAVE_API_KEY"})
        self.assertEqual(environment.writes, ["BRAVE_API_KEY"])
        self.assertEqual(set(environment), {"SERPER_API_KEY", "BRAVE_API_KEY"})

    def test_cli_redacts_compound_credential_names_without_hiding_ordinary_prose(self) -> None:
        ordinary = "The client secret policy and private key format are documented."
        rendered = cli._json_text(
            {
                "error": "access_token=cli-access refresh-token=cli-refresh client_secret: cli-secret",
                "url": "https://example.test/article?client-key=cli-client-key&private_key=cli-private-key",
                "nested": {
                    "content": "private-key=payload-private-key",
                    "client_key": "mapping-client-key",
                    "description": ordinary,
                },
                "list": [
                    "Bearer cli-bearer-token",
                    "X-Appbuilder-Authorization: Bearer cli-appbuilder-token",
                    "Authorization: [Bearer cli-bracket-token]",
                    'Authorization: [Bearer "cli-quoted-bracket-token"]',
                    "Basic {'cli-quoted-basic-token'}",
                ],
            }
        )

        for secret in (
            "cli-access",
            "cli-refresh",
            "cli-secret",
            "cli-client-key",
            "cli-private-key",
            "payload-private-key",
            "mapping-client-key",
            "cli-bearer-token",
            "cli-appbuilder-token",
            "cli-bracket-token",
            "cli-quoted-bracket-token",
            "cli-quoted-basic-token",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn(ordinary, rendered)
        self.assertIn("Authorization: [redacted]", rendered)
        self.assertNotIn("]]]", rendered)

    def test_cli_shared_sanitizer_keeps_bare_prose_and_hides_header_url_credentials(self) -> None:
        ordinary_bearer = "The bearer of good news arrived."
        ordinary_basic = "Basic information is available."
        ordinary_authentication = "Bearer authentication is a guide."
        rendered = cli._json_text(
            {
                "message": 'Authorization: Digest username="reader", nonce="cli-digest-secret"',
                "app_message": "X-Appbuilder-Authorization: OAuth cli-oauth-secret",
                "url": (
                    "https://reader:cli-password@example.test/path?"
                    "access%5Ftoken=cli-encoded-token&X-Amz-Signature=cli-amz-signature&"
                    "credential=cli-credential&%73%69%67=cli-sig&jwt=cli-jwt&"
                    "access_key=cli-access-key&aws_access_key_id=cli-aws-access-key-id&keep=public"
                ),
                "fragment_url": (
                    "https://example.test/callback#id_token=cli-id-token&access_token=cli-fragment-access&"
                    "jwt=cli-fragment-jwt&sig=cli-fragment-sig&section=overview"
                ),
                "nested_url": (
                    "https://gateway.test/redirect?next=https://reader:cli-nested-password@target.test/path?"
                    "access_token=cli-nested-token"
                ),
                "path_url": (
                    "https://gateway.test/path/https://reader:cli-path-password@target.test/article?"
                    "token=cli-path-token#id_token=cli-path-id-token"
                ),
                "protocol_relative_url": "//reader:cli-protocol-password@target.test/path?token=cli-protocol-token",
                "nested": [
                    ordinary_bearer,
                    ordinary_basic,
                    ordinary_authentication,
                    "Useful article text. Bearer cli-inline-token should not leak.",
                    "key=cli-standalone-key",
                ],
                "cookie": "cli-cookie-value",
            }
        )

        for secret in (
            "cli-digest-secret",
            "cli-oauth-secret",
            "cli-password",
            "cli-encoded-token",
            "cli-amz-signature",
            "cli-credential",
            "cli-sig",
            "cli-jwt",
            "cli-access-key",
            "cli-aws-access-key-id",
            "cli-id-token",
            "cli-fragment-access",
            "cli-fragment-jwt",
            "cli-fragment-sig",
            "cli-nested-password",
            "cli-nested-token",
            "cli-path-password",
            "cli-path-token",
            "cli-path-id-token",
            "cli-protocol-password",
            "cli-protocol-token",
            "cli-inline-token",
            "cli-standalone-key",
            "cli-cookie-value",
        ):
            self.assertNotIn(secret, rendered)
        for ordinary in (ordinary_bearer, ordinary_basic, ordinary_authentication):
            self.assertIn(ordinary, rendered)
        self.assertIn("Authorization: [redacted]", rendered)
        self.assertIn("X-Appbuilder-Authorization: [redacted]", rendered)
        self.assertIn("https://[redacted]@example.test/path", rendered)
        self.assertIn("#id_token=[redacted]&access_token=[redacted]&jwt=[redacted]&sig=[redacted]&section=overview", rendered)
        self.assertIn("next=https://[redacted]@target.test/path?access_token=[redacted]", rendered)
        self.assertIn("//[redacted]@target.test/path?token=[redacted]", rendered)
        self.assertIn("/path/https://[redacted]@target.test/article?token=[redacted]#id_token=[redacted]", rendered)

    def test_doctor_has_only_safe_status_data_and_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.env"
            path.write_text("SERPER_API_KEY=test-only-secret-value\n", encoding="utf-8")

            class NoEngine:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    raise AssertionError("doctor must not construct an engine")

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", NoEngine),
            ):
                status, stdout, stderr = self._run_cli(["doctor"])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("test-only-secret-value", stdout)
        self.assertNotIn("SERPER_API_KEY", stdout)
        payload = json.loads(stdout)
        self.assertTrue(payload["providers"]["serper"]["configured"])
        self.assertEqual(payload["providers"]["serper"], {"configured": True})
        self.assertTrue(payload["secrets_file"]["exists"])

    def test_configure_writes_a_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "secrets.env"
            credential = ' leading space # "quoted" and \\slashes\\ '
            responses = iter([credential, "", "", "", "", "", "", ""])
            with (
                patch.object(secrets, "secrets_path", return_value=path),
                patch.object(secrets.getpass, "getpass", side_effect=lambda _: next(responses)),
            ):
                saved_path = secrets.configure_secrets()

            self.assertEqual(saved_path, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            environment: dict[str, str] = {}
            self.assertEqual(load_secrets(path, environment), {"SERPER_API_KEY"})
            self.assertEqual(environment["SERPER_API_KEY"], credential)

    def test_nul_credentials_are_rejected_or_safely_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured_path = root / "config" / "secrets.env"
            with (
                patch.object(secrets, "secrets_path", return_value=configured_path),
                patch.object(secrets.getpass, "getpass", return_value="invalid\0credential"),
            ):
                with self.assertRaisesRegex(ValueError, "NUL") as raised:
                    secrets.configure_secrets()

            self.assertNotIn("invalid\0credential", str(raised.exception))
            self.assertFalse(configured_path.exists())

            hand_edited_path = root / "hand-edited.env"
            hand_edited_path.write_text('SERPER_API_KEY="invalid\\u0000credential"\n', encoding="utf-8")
            environment: dict[str, str] = {}
            self.assertEqual(load_secrets(hand_edited_path, environment), set())
            self.assertEqual(environment, {})

            class RejectingEnvironment(dict[str, str]):
                def __setitem__(self, key: str, value: str) -> None:
                    raise RuntimeError("environment rejected value")

            hand_edited_path.write_text('SERPER_API_KEY="ordinary-value"\n', encoding="utf-8")
            self.assertEqual(load_secrets(hand_edited_path, RejectingEnvironment()), set())

    def test_help_and_explain_route_work_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with patch.dict(os.environ, {}, clear=True), patch("searchx.secrets.secrets_path", return_value=path):
                status, stdout, stderr = self._run_cli(["--help"])
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertIn("explain-route", stdout)

                status, stdout, stderr = self._run_cli(["explain-route", "latest OpenAI news"])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["plan"]["mode"], "news")

    def test_explain_route_rejects_blank_queries_before_routing(self) -> None:
        class NoRouter:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("blank explain-route query must not construct a router")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "Router", NoRouter),
            ):
                status, stdout, stderr = self._run_cli(["explain-route", "   "])

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "query must be a non-empty string")

    def test_search_and_provider_reject_blank_queries_before_engine_work(self) -> None:
        class NoRouter:
            def plan(self, *args: object, **kwargs: object) -> SearchPlan:
                raise AssertionError("blank search query must not route")

        class NoProvider:
            configured = True

            def __init__(self) -> None:
                self.calls = 0

            def search(self, query: str, **kwargs: object) -> ProviderCall:
                self.calls += 1
                return ProviderCall(provider="fake", query=query)

        provider = NoProvider()
        engine = SearchEngine.__new__(SearchEngine)
        engine.router = NoRouter()
        engine.providers = {"fake": provider}

        with self.assertRaisesRegex(ValueError, "non-empty"):
            engine.search("  ")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            engine.provider_search("fake", "\t")
        self.assertEqual(provider.calls, 0)

        class NoEngine:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("blank CLI query must not construct an engine")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", NoEngine),
            ):
                for arguments in (["search", "   "], ["provider", "fake", "\t"]):
                    status, stdout, stderr = self._run_cli(arguments)
                    self.assertEqual(status, 1)
                    self.assertEqual(stdout, "")
                    self.assertEqual(json.loads(stderr)["error"], "query must be a non-empty string")

    def test_provider_forwards_only_explicit_options(self) -> None:
        received: dict[str, object] = {}

        class FakeEngine:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def provider_search(self, provider: str, query: str, **options: object) -> ProviderCall:
                received.update(provider=provider, query=query, options=options)
                return ProviderCall(provider=provider, query=query)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", FakeEngine),
            ):
                status, stdout, stderr = self._run_cli(
                    [
                        "provider",
                        "github",
                        "SearchX",
                        "--limit",
                        "2",
                        "--category",
                        "issues",
                        "--depth",
                        "auto",
                        "--full-content",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(received["provider"], "github")
        self.assertEqual(received["query"], "SearchX")
        self.assertEqual(
            received["options"],
            {"limit": 2, "category": "issues", "depth": "auto", "full_content": True},
        )
        self.assertEqual(json.loads(stdout)["provider"], "github")

    def test_fetch_exit_status_requires_an_explicit_failure_flag(self) -> None:
        class FakeEngine:
            result: dict[str, object] = {}

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def fetch(self, url: str, provider: str = "auto") -> dict[str, object]:
                return self.result

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", FakeEngine),
            ):
                for result, expected_status in (
                    ({"provider": "exa", "results": []}, 0),
                    ({"ok": True, "provider": "exa"}, 0),
                    ({"ok": False, "error": "no provider"}, 1),
                ):
                    FakeEngine.result = result
                    status, stdout, stderr = self._run_cli(["fetch", "https://example.test"])
                    self.assertEqual(status, expected_status)
                    self.assertEqual(stderr, "")
                    self.assertEqual(json.loads(stdout), result)

    def test_search_exit_status_tracks_successful_provider_calls(self) -> None:
        plan = SearchPlan(mode="web", primary=["serper"], fallback=[])

        class FakeEngine:
            response = SearchResponse(
                query="SearchX",
                mode="web",
                plan=plan,
                results=[],
                calls=[],
                elapsed_ms=0.0,
            )

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def search(self, *args: object, **kwargs: object) -> SearchResponse:
                return self.response

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", FakeEngine),
            ):
                status, stdout, stderr = self._run_cli(["search", "SearchX"])
                self.assertEqual(status, 1)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["providers"], [])

                FakeEngine.response = SearchResponse(
                    query="SearchX",
                    mode="web",
                    plan=plan,
                    results=[],
                    calls=[ProviderCall(provider="serper", query="SearchX")],
                    elapsed_ms=0.0,
                )
                status, stdout, stderr = self._run_cli(["search", "SearchX"])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["providers"][0]["result_count"], 0)

    def test_multi_search_evidence_and_research_plan_cli_wiring(self) -> None:
        plan = SearchPlan(mode="web", primary=["serper"], fallback=[])

        def response(query: str, status: str) -> SearchResponse:
            return SearchResponse(
                query=query,
                mode="web",
                plan=plan,
                results=[],
                calls=[ProviderCall(provider="serper", query=query, status=status)],
                elapsed_ms=0.0,
            )

        class FakeEngine:
            all_error = False
            evidence_fetch_ok = True
            multi_received: dict[str, object] = {}
            evidence_received: dict[str, object] = {}

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def multi_search(self, queries: list[str], **kwargs: object) -> list[SearchResponse]:
                type(self).multi_received = {"queries": queries, **kwargs}
                statuses = ["error", "error"] if self.all_error else ["error", "ok"]
                return [response(query, status) for query, status in zip(queries, statuses, strict=True)]

            def collect_evidence(self, query: str, **kwargs: object) -> dict[str, object]:
                type(self).evidence_received = {"query": query, **kwargs}
                return {
                    "query": query,
                    "verification_status": "not_verified",
                    "search": response(query, "error").to_dict(),
                    "evidence": [
                        {
                            "url": "https://example.test/source",
                            "result": None,
                            "fetch": {
                                "ok": self.evidence_fetch_ok,
                                "payload": {"headers": {"Authorization": "Bearer cli-only-secret"}},
                            },
                        }
                    ],
                    "counts": {"selected_urls": 1},
                }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", FakeEngine),
            ):
                status, stdout, stderr = self._run_cli(
                    ["multi-search", "first", "second", "--limit", "2", "--all-fallbacks"]
                )
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                payload = json.loads(stdout)
                self.assertEqual([item["query"] for item in payload["responses"]], ["first", "second"])
                self.assertEqual([item["ok"] for item in payload["responses"]], [False, True])
                self.assertEqual(FakeEngine.multi_received["limit"], 2)
                self.assertTrue(FakeEngine.multi_received["all_fallbacks"])

                FakeEngine.all_error = True
                status, stdout, stderr = self._run_cli(["multi-search", "first", "second"])
                self.assertEqual(status, 1)
                self.assertEqual(stderr, "")
                self.assertFalse(json.loads(stdout)["ok"])

                status, stdout, stderr = self._run_cli(
                    ["evidence", "evidence query", "--url", "https://example.test/source", "--fetch-limit", "1"]
                )

            self.assertEqual(status, 0)
            self.assertEqual(stderr, "")
            self.assertNotIn("cli-only-secret", stdout)
            self.assertEqual(FakeEngine.evidence_received["urls"], ["https://example.test/source"])
            self.assertEqual(FakeEngine.evidence_received["fetch_limit"], 1)

            FakeEngine.evidence_fetch_ok = False
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", FakeEngine),
            ):
                status, stdout, stderr = self._run_cli(["evidence", "evidence query"])
            self.assertEqual(status, 1)
            self.assertEqual(stderr, "")
            self.assertFalse(json.loads(stdout)["evidence"][0]["fetch"]["ok"])

            class NoEngine:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    raise AssertionError("research-plan must stay offline and avoid engine construction")

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", NoEngine),
            ):
                status, stdout, stderr = self._run_cli(["research-plan", "offline query", "--mode", "web"])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        research = json.loads(stdout)
        self.assertEqual(research["plan"]["mode"], "web")
        self.assertEqual([step["step"] for step in research["workflow"]], ["search", "evidence-gap-check", "optional-fetch", "cross-check"])

    def test_multi_search_and_evidence_reject_blank_queries_before_engine(self) -> None:
        class NoEngine:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("blank query must not construct an engine")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", NoEngine),
            ):
                for arguments, expected_error in (
                    (["multi-search", "first", " \t"], "query must be a non-empty string"),
                    (["evidence", "\n\t"], "query must be a non-empty string"),
                    (["evidence", "valid query", "--url", " \t"], "url must be a non-empty string"),
                    (["fetch", " \t"], "url must be a non-empty string"),
                ):
                    with self.subTest(arguments=arguments):
                        status, stdout, stderr = self._run_cli(arguments)
                        self.assertEqual(status, 1)
                        self.assertEqual(stdout, "")
                        self.assertEqual(json.loads(stderr)["error"], expected_error)

    def test_search_fallback_uses_fused_primary_result_count(self) -> None:
        plan = SearchPlan(
            mode="web",
            primary=["primary"],
            fallback=["fallback"],
            min_results=2,
        )

        class FakeRouter:
            def plan(self, *args: object, **kwargs: object) -> SearchPlan:
                return plan

            def weights_for(self, mode: str) -> dict[str, float]:
                return {}

        primary = ProviderCall(
            provider="primary",
            query="query",
            results=[
                SearchResult("first", "https://same.example/article?utm_source=test", provider="primary", rank=1),
                SearchResult("duplicate", "https://www.same.example/article", provider="primary", rank=2),
                SearchResult("same domain", "https://same.example/other", provider="primary", rank=3),
            ],
        )
        fallback = ProviderCall(
            provider="fallback",
            query="query",
            results=[SearchResult("fallback", "https://other.example/article", provider="fallback", rank=1)],
        )
        engine = SearchEngine.__new__(SearchEngine)
        engine.settings = Settings(result_limit=5, domain_cap=1)
        engine.router = FakeRouter()
        invocation_order: list[list[str]] = []

        def run_many(names: list[str], *args: object, **kwargs: object) -> list[ProviderCall]:
            invocation_order.append(names)
            return [primary] if names == plan.primary else [fallback]

        engine._run_many = run_many  # type: ignore[method-assign]
        engine.configured_providers = lambda: ["primary", "fallback"]  # type: ignore[method-assign]

        response = engine.search("query", limit=2)

        self.assertEqual(invocation_order, [["primary"], ["fallback"]])
        self.assertEqual(len(response.results), 2)
        self.assertEqual({result.metadata["domain"] for result in response.results}, {"same.example", "other.example"})

    def test_search_lower_limit_caps_fallback_threshold(self) -> None:
        plan = SearchPlan(mode="web", primary=["primary"], fallback=["fallback"], min_results=5)

        class FakeRouter:
            def plan(self, *args: object, **kwargs: object) -> SearchPlan:
                return plan

            def weights_for(self, mode: str) -> dict[str, float]:
                return {}

        engine = SearchEngine.__new__(SearchEngine)
        engine.settings = Settings(result_limit=5)
        engine.router = FakeRouter()
        primary = ProviderCall(
            provider="primary",
            query="query",
            results=[SearchResult("only result", "https://one.example/article", provider="primary", rank=1)],
        )
        invocation_order: list[list[str]] = []

        def run_many(names: list[str], *args: object, **kwargs: object) -> list[ProviderCall]:
            invocation_order.append(names)
            if names == plan.fallback:
                self.fail("a one-result limit should satisfy the capped threshold")
            return [primary]

        engine._run_many = run_many  # type: ignore[method-assign]
        engine.configured_providers = lambda: ["primary"]  # type: ignore[method-assign]

        response = engine.search("query", limit=1)

        self.assertEqual(invocation_order, [["primary"]])
        self.assertEqual(len(response.results), 1)

    def test_progressive_search_intensities_and_hard_budgets(self) -> None:
        plan = SearchPlan(
            mode="web",
            primary=["p1", "p2", "p3"],
            fallback=["f1", "f2"],
            min_results=2,
        )

        class FakeRouter:
            def plan(self, *args: object, **kwargs: object) -> SearchPlan:
                return plan

            def weights_for(self, mode: str) -> dict[str, float]:
                return {}

        def make_call(provider: str, *, with_result: bool = True) -> ProviderCall:
            results = (
                [SearchResult(provider, f"https://{provider}.example/article", provider=provider, rank=1)]
                if with_result
                else []
            )
            return ProviderCall(provider=provider, query="query", results=results)

        def engine_with_calls(empty_first: bool = False) -> tuple[SearchEngine, list[list[str]]]:
            engine = SearchEngine.__new__(SearchEngine)
            engine.settings = Settings(result_limit=5)
            engine.router = FakeRouter()
            invocations: list[list[str]] = []

            def run_many(names: list[str], *args: object, **kwargs: object) -> list[ProviderCall]:
                invocations.append(list(names))
                return [make_call(name, with_result=not (empty_first and name == "p1")) for name in names]

            engine._run_many = run_many  # type: ignore[method-assign]
            engine.configured_providers = lambda: ["p1", "p2", "p3", "f1", "f2"]  # type: ignore[method-assign]
            return engine, invocations

        quick_engine, quick_invocations = engine_with_calls(empty_first=True)
        quick = quick_engine.search("query", intensity="quick")
        self.assertEqual(quick_invocations, [["p1"], ["p2"]])
        self.assertEqual(quick.execution["stop_reason"], "quick_result_found")  # type: ignore[index]
        self.assertEqual(quick.execution["provider_call_count"], 2)  # type: ignore[index]

        adaptive_engine, adaptive_invocations = engine_with_calls()
        adaptive = adaptive_engine.search("query", intensity="adaptive")
        self.assertEqual(adaptive_invocations, [["p1"], ["p2", "p3"]])
        self.assertEqual(adaptive.execution["stop_reason"], "evidence_target_met")  # type: ignore[index]
        self.assertEqual(adaptive.execution["stage_count"], 2)  # type: ignore[index]

        deep_engine, deep_invocations = engine_with_calls()
        deep = deep_engine.search("query", intensity="deep")
        self.assertEqual(deep_invocations, [["p1", "p2", "p3"], ["f1", "f2"]])
        self.assertEqual(deep.execution["stop_reason"], "intensity_complete")  # type: ignore[index]
        self.assertEqual(deep.execution["provider_call_count"], 5)  # type: ignore[index]

        budget_engine, budget_invocations = engine_with_calls(empty_first=True)
        budgeted = budget_engine.search("query", max_provider_calls=1, max_stages=1)
        self.assertEqual(budget_invocations, [["p1"]])
        self.assertEqual(budgeted.execution["stop_reason"], "max_provider_calls_reached")  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "max_provider_calls"):
            budget_engine.search("query", max_provider_calls=0)

    def test_research_plan_exposes_deterministic_strategy_budget(self) -> None:
        plan = SearchPlan(
            mode="official",
            primary=["serper", "exa"],
            fallback=["brave", "tavily"],
            min_results=8,
        )

        research = build_research_plan(
            "official specification",
            plan,
            intensity="adaptive",
            max_provider_calls=3,
            max_stages=2,
        )

        self.assertEqual(
            research["search_strategy"],
            {
                "intensity": "adaptive",
                "max_provider_calls": 3,
                "max_stages": 2,
                "stage_policy": "start with one provider and escalate only for a named evidence gap",
                "success_criteria": {
                    "minimum_usable_results": 8,
                    "minimum_distinct_domains": 2,
                },
                "hard_rule": "the engine enforces budgets; the caller may request escalation but may not exceed them",
            },
        )
        self.assertEqual(research["stop_criteria"]["max_provider_calls"], 3)

    def test_search_fusion_keeps_provider_call_results_raw(self) -> None:
        plan = SearchPlan(mode="web", primary=["primary", "secondary"], fallback=[], min_results=1)

        class FakeRouter:
            def plan(self, *args: object, **kwargs: object) -> SearchPlan:
                return plan

            def weights_for(self, mode: str) -> dict[str, float]:
                return {}

        source_url = "https://www.example.test/article/?utm_source=test"
        source = SearchResult(
            "primary result",
            source_url,
            provider="primary",
            rank=5,
            metadata={"source_marker": "primary"},
        )
        duplicate = SearchResult(
            "secondary result",
            "https://example.test/article",
            provider="secondary",
            rank=8,
            metadata={"source_marker": "secondary"},
        )
        primary = ProviderCall(provider="primary", query="query", results=[source])
        secondary = ProviderCall(provider="secondary", query="query", results=[duplicate])
        engine = SearchEngine.__new__(SearchEngine)
        engine.settings = Settings(result_limit=5)
        engine.router = FakeRouter()
        engine._run_many = lambda *args, **kwargs: [primary, secondary]  # type: ignore[method-assign]
        engine.configured_providers = lambda: ["primary", "secondary"]  # type: ignore[method-assign]

        response = engine.search("query")

        self.assertEqual(response.results[0].provider, "primary+secondary")
        self.assertEqual(source.url, source_url)
        self.assertEqual(source.provider, "primary")
        self.assertEqual(source.rank, 5)
        self.assertEqual(source.metadata, {"source_marker": "primary"})
        self.assertEqual(duplicate.provider, "secondary")
        self.assertEqual(duplicate.rank, 8)
        self.assertEqual(duplicate.metadata, {"source_marker": "secondary"})

    def test_limit_validation_and_settings_environment_bounds(self) -> None:
        engine = SearchEngine.__new__(SearchEngine)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            engine.search("query", limit=0)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            engine.search("query", limit=-1)

        with tempfile.TemporaryDirectory() as directory:
            missing_secrets = Path(directory) / "missing.env"
            with (
                patch.dict(
                    os.environ,
                    {
                        "SEARCHX_TIMEOUT": "Infinity",
                        "SEARCHX_RETRIES": "-1",
                        "SEARCHX_MAX_WORKERS": "0",
                        "SEARCHX_RESULT_LIMIT": "NaN",
                    },
                    clear=True,
                ),
                patch("searchx.secrets.secrets_path", return_value=missing_secrets),
            ):
                invalid = Settings.load()
            with (
                patch.dict(
                    os.environ,
                    {
                        "SEARCHX_TIMEOUT": "2.5",
                        "SEARCHX_RETRIES": "0",
                        "SEARCHX_MAX_WORKERS": "3",
                        "SEARCHX_RESULT_LIMIT": "7",
                    },
                    clear=True,
                ),
                patch("searchx.secrets.secrets_path", return_value=missing_secrets),
            ):
                valid = Settings.load()

        self.assertEqual((invalid.timeout, invalid.retries, invalid.max_workers, invalid.result_limit), (12.0, 1, 4, 10))
        self.assertEqual((valid.timeout, valid.retries, valid.max_workers, valid.result_limit), (2.5, 0, 3, 7))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"

            class NoEngine:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    raise AssertionError("invalid limits must fail during argument parsing")

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", NoEngine),
            ):
                status, stdout, stderr = self._run_cli(["search", "query", "--limit", "0"])

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("must be greater than zero", stderr)

    def test_environment_profile_routes_and_explicit_profile_overrides_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets_file = root / "missing.env"
            environment_profile = root / "environment.json"
            explicit_profile = root / "explicit.json"
            environment_profile.write_text(
                json.dumps(
                    {
                        "provider_weights": {"serper": 0.7},
                        "routes": {"web": {"primary": ["brave"], "fallback": ["tavily"], "min_results": 3}},
                        "mode_provider_weights": {"web": {"brave": 1.25}},
                    }
                ),
                encoding="utf-8",
            )
            explicit_profile.write_text(
                json.dumps(
                    {
                        "routes": {"web": {"primary": ["github"], "fallback": [], "min_results": 1}},
                        "mode_provider_weights": {"web": {"github": 1.4}},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"SEARCHX_PROFILE": str(environment_profile)}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=secrets_file),
            ):
                settings = Settings.load()
                environment_router = Router(settings)
                self.assertEqual(environment_router.plan("ordinary query").primary, ["brave"])
                self.assertEqual(environment_router.weights_for("web")["brave"], 1.25)
                self.assertEqual(settings.provider_weights["serper"], 0.7)

                status, stdout, stderr = self._run_cli(["explain-route", "ordinary query"])
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["plan"]["primary"], ["brave"])

                status, stdout, stderr = self._run_cli(
                    ["explain-route", "ordinary query", "--profile", str(explicit_profile)]
                )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["plan"]["primary"], ["github"])

    def test_malformed_profile_schemas_are_ignored(self) -> None:
        malformed_profiles: list[object] = [
            [],
            {"provider_weights": [], "routes": [], "mode_provider_weights": []},
            {
                "provider_weights": {"serper": []},
                "routes": {"web": {"primary": "brave", "fallback": {"bad": True}, "min_results": "many"}},
                "mode_provider_weights": {"web": {"brave": []}},
            },
            {
                "routes": {"web": {"primary": ["brave", 1], "fallback": [None], "min_results": True}},
                "mode_provider_weights": {"web": ["not", "a", "mapping"]},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profile.json"
            secrets_file = root / "missing.env"
            with patch.dict(os.environ, {}, clear=True), patch("searchx.secrets.secrets_path", return_value=secrets_file):
                for profile in malformed_profiles:
                    path.write_text(json.dumps(profile), encoding="utf-8")
                    settings = Settings.load(str(path))
                    router = Router(settings, str(path))
                    plan = router.plan("ordinary query")
                    self.assertEqual(settings.provider_weights["serper"], 1.0)
                    self.assertEqual(plan.primary, ["serper", "brave"])
                    self.assertEqual(plan.fallback, ["tavily", "exa"])

    def test_nonfinite_weights_and_negative_min_results_are_ignored(self) -> None:
        profile = {
            "provider_weights": {
                "serper": float("nan"),
                "brave": float("inf"),
                "tavily": "-Infinity",
                "exa": "0.8",
            },
            "routes": {
                "web": {"min_results": -1},
                "fresh": {"min_results": 0},
            },
            "mode_provider_weights": {
                "web": {
                    "serper": float("nan"),
                    "brave": "Infinity",
                    "tavily": "-Inf",
                    "exa": "1.3",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            secrets_file = root / "missing.env"
            with patch.dict(os.environ, {}, clear=True), patch("searchx.secrets.secrets_path", return_value=secrets_file):
                settings = Settings.load(str(path))
                router = Router(settings, str(path))

        self.assertEqual(settings.provider_weights["serper"], 1.0)
        self.assertEqual(settings.provider_weights["brave"], 1.0)
        self.assertEqual(settings.provider_weights["tavily"], 0.95)
        self.assertEqual(settings.provider_weights["exa"], 0.8)
        self.assertEqual(router.weights_for("web")["exa"], 1.3)
        self.assertEqual(router.plan("ordinary query").min_results, 8)
        self.assertEqual(router.plan("latest query").min_results, 0)

    def test_benchmark_zero_cap_and_cli_negative_cap(self) -> None:
        class FakeBenchmarkEngine:
            def __init__(self) -> None:
                self.calls = 0

            def configured_providers(self) -> list[str]:
                return ["serper"]

            def provider_search(self, provider: str, query: str, **kwargs: object) -> ProviderCall:
                self.calls += 1
                return ProviderCall(provider=provider, query=query)

        cases = [
            BenchmarkCase(id="one", scenario="web", query="first"),
            BenchmarkCase(id="two", scenario="web", query="second"),
        ]
        fake_engine = FakeBenchmarkEngine()
        runner = BenchmarkRunner(fake_engine)  # type: ignore[arg-type]
        zero = runner.run(cases, max_cases=0)
        uncapped = runner.run(cases, max_cases=None, workers=1)

        self.assertEqual((zero["case_count"], zero["call_count"], fake_engine.calls), (0, 0, 2))
        self.assertEqual((uncapped["case_count"], uncapped["call_count"]), (2, 2))
        with self.assertRaisesRegex(ValueError, "non-negative"):
            runner.run(cases, max_cases=-1)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"

            class EmptyEngine:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def configured_providers(self) -> list[str]:
                    return []

            class NoEngine:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    raise AssertionError("negative caps must fail during argument parsing")

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", EmptyEngine),
            ):
                status, stdout, stderr = self._run_cli(["bench", "--max-cases", "0"])
                self.assertEqual(status, 1)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["case_count"], 0)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=path),
                patch.object(cli, "SearchEngine", NoEngine),
            ):
                status, stdout, stderr = self._run_cli(["bench", "--max-cases", "-1"])

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("must be zero or greater", stderr)

    def test_benchmark_exception_rows_preserve_case_context(self) -> None:
        class FailingEngine:
            def configured_providers(self) -> list[str]:
                return ["serper"]

            def provider_search(self, provider: str, query: str, **kwargs: object) -> ProviderCall:
                raise RuntimeError("test provider failure")

        case = BenchmarkCase(id="failure-case", scenario="web", query="specific benchmark query")
        report = BenchmarkRunner(FailingEngine()).run([case], workers=1)  # type: ignore[arg-type]
        row = report["rows"][0]

        self.assertEqual(row["case_id"], "failure-case")
        self.assertEqual(row["scenario"], "web")
        self.assertEqual(row["query"], "specific benchmark query")
        self.assertEqual(row["http_status"], None)
        self.assertEqual(row["usage"], {})
        self.assertEqual(row["metadata"], {})
        self.assertEqual(report["summary"]["web"]["serper"]["calls"], 1)

    def test_bench_cli_validates_filters_and_reports_nonzero_without_successes(self) -> None:
        class BenchmarkEngine:
            response_status = "ok"

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def configured_providers(self) -> list[str]:
                return ["serper"]

            def provider_search(self, provider: str, query: str, **kwargs: object) -> ProviderCall:
                return ProviderCall(provider=provider, query=query, status=self.response_status)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets_file = root / "missing.env"
            cases = root / "cases.json"
            cases.write_text(
                json.dumps([{"id": "case", "scenario": "quick", "query": "query", "providers": ["serper"]}]),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=secrets_file),
                patch.object(cli, "SearchEngine", BenchmarkEngine),
            ):
                status, stdout, stderr = self._run_cli(["bench", "--cases", str(cases), "--scenario", "unknown"])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("invalid choice", stderr)

                status, stdout, stderr = self._run_cli(["bench", "--cases", str(cases), "--provider", "unknown"])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("invalid choice", stderr)

                BenchmarkEngine.response_status = "error"
                status, stdout, stderr = self._run_cli(["bench", "--cases", str(cases)])
                self.assertEqual(status, 1)
                self.assertEqual(stderr, "")
                failed_report = json.loads(stdout)
                self.assertEqual(failed_report["scheduled_call_count"], 1)
                self.assertEqual(failed_report["call_count"], 1)

                BenchmarkEngine.response_status = "ok"
                status, stdout, stderr = self._run_cli(["bench", "--cases", str(cases)])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        successful_report = json.loads(stdout)
        self.assertEqual(successful_report["call_count"], 1)
        self.assertEqual(successful_report["summary"]["quick"]["serper"]["avg_results"], 0.0)

    def test_tune_rejects_empty_failed_and_route_less_reports(self) -> None:
        reports = [
            ({}, "no calls"),
            ({"call_count": 0, "rows": [], "summary": {}}, "no calls"),
            ({"call_count": 1, "rows": [{"status": "error"}], "summary": {}}, "no successful calls"),
            ({"call_count": 1, "rows": [{"status": "ok"}], "summary": {}}, "no usable routes"),
            (
                {
                    "call_count": 1,
                    "rows": [{"status": "ok"}],
                    "summary": {
                        "unknown": {"serper": {"success_rate": 1.0, "quality": 0.8, "latency_ms": 10.0}}
                    },
                },
                "unknown scenario",
            ),
            (
                {
                    "call_count": 1,
                    "rows": [{"status": "ok"}],
                    "summary": {"web": {"serper": {"success_rate": 1.0, "quality": 1.1, "latency_ms": 10.0}}},
                },
                "invalid provider metrics",
            ),
            (
                {
                    "call_count": 1,
                    "rows": [{"status": "ok"}],
                    "summary": {
                        "web": {"serper": {"success_rate": 1.0, "quality": 0.8, "latency_ms": 10.0, "avg_results": 0.0}}
                    },
                },
                "no usable routes",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets_file = root / "missing.env"
            report_path = root / "report.json"
            with patch.dict(os.environ, {}, clear=True), patch("searchx.secrets.secrets_path", return_value=secrets_file):
                for report, message in reports:
                    with self.subTest(message=message):
                        report_path.write_text(json.dumps(report), encoding="utf-8")
                        status, stdout, stderr = self._run_cli(["tune", str(report_path)])
                        self.assertEqual(status, 1)
                        self.assertEqual(stdout, "")
                        self.assertIn(message, stderr)

    def test_tune_writes_profile_from_mixed_avg_results_report(self) -> None:
        report_data = {
            "call_count": 2,
            "rows": [{"status": "ok"}, {"status": "ok"}],
            "summary": {
                "web": {
                    "serper": {"success_rate": 1.0, "quality": 1.0, "latency_ms": 10.0, "avg_results": 0.0},
                    "brave": {"success_rate": 0.5, "quality": 0.2, "latency_ms": 10.0, "avg_results": 1.0},
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets_file = root / "missing.env"
            report_path = root / "report.json"
            profile_path = root / "profile.json"
            report_path.write_text(json.dumps(report_data), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("searchx.secrets.secrets_path", return_value=secrets_file):
                status, stdout, stderr = self._run_cli(["tune", str(report_path), "--output", str(profile_path)])

            self.assertEqual(status, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["routes"]["web"]["primary"], ["brave"])
            self.assertEqual(set(payload["mode_provider_weights"]["web"]), {"brave"})
            self.assertEqual(json.loads(profile_path.read_text(encoding="utf-8"))["routes"]["web"]["primary"], ["brave"])

    def test_remaining_commands_have_non_network_basic_wiring(self) -> None:
        class FakeEngine:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def configured_providers(self) -> list[str]:
                return []

            def fetch(self, url: str, provider: str = "auto") -> dict[str, object]:
                return {"ok": True, "url": url, "provider": provider}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets_file = root / "missing.env"
            report = root / "report.json"
            profile = root / "profile.json"
            report.write_text(
                json.dumps(
                    {
                        "call_count": 1,
                        "rows": [{"status": "ok"}],
                        "summary": {
                            "web": {
                                "serper": {
                                    "success_rate": 1.0,
                                    "quality": 0.8,
                                    "unique_ratio": 0.2,
                                    "latency_ms": 100.0,
                                    "avg_results": 1.0,
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("searchx.secrets.secrets_path", return_value=secrets_file),
                patch.object(cli, "SearchEngine", FakeEngine),
                patch.object(cli, "configure_secrets", return_value=secrets_file),
            ):
                status, stdout, stderr = self._run_cli(["configure"])
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertTrue(json.loads(stdout)["ok"])

                status, stdout, stderr = self._run_cli(["fetch", "https://example.test", "--provider", "exa"])
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["provider"], "exa")

                status, stdout, stderr = self._run_cli(["bench", "--max-cases", "1"])
                self.assertEqual(status, 1)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["call_count"], 0)

                status, stdout, stderr = self._run_cli(["tune", str(report), "--output", str(profile)])

            self.assertEqual(status, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(json.loads(stdout)["profile_written"])
            self.assertTrue(profile.exists())


if __name__ == "__main__":
    unittest.main()
