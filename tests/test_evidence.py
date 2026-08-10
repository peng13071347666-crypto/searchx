from __future__ import annotations

from datetime import UTC, datetime
import unittest
from urllib.parse import quote_plus

from searchx.config import Settings
from searchx.engine import SearchEngine
from searchx.evidence import build_evidence_signals, parse_standard_timestamp
from searchx.models import (
    FetchAttempt,
    FetchOutcome,
    ProviderCall,
    SearchPlan,
    SearchResponse,
    SearchResult,
    redact_sensitive_text,
)


def _response(query: str, *, status: str = "ok", results: list[SearchResult] | None = None) -> SearchResponse:
    return SearchResponse(
        query=query,
        mode="web",
        plan=SearchPlan(mode="web", primary=["serper"], fallback=[]),
        results=results or [],
        calls=[ProviderCall(provider="serper", query=query, status=status)],
        elapsed_ms=1.0,
    )


class EvidenceSignalTests(unittest.TestCase):
    def test_shared_sanitizer_redacts_quoted_wrapper_credentials_without_artifacts(self) -> None:
        cases = {
            'Authorization: [Bearer "q1"]': "Authorization: [redacted]",
            "Authorization: (Bearer 'q4')": "Authorization: (redacted)",
            'Bearer ["q3"]': 'Bearer ["redacted"]',
            'Bearer ["public", "structured-secret"]': 'Bearer ["redacted"]',
            "Basic {kind: structured-secret}": "Basic {redacted}",
            "X-Appbuilder-Authorization: {Basic 'q14'}": "X-Appbuilder-Authorization: {redacted}",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                sanitized = redact_sensitive_text(source)
                self.assertEqual(sanitized, expected)
                self.assertNotIn("]]]", sanitized)
                self.assertNotIn("q", sanitized)

    def test_shared_sanitizer_redacts_complete_headers_urls_and_token_shaped_bare_values(self) -> None:
        headers = {
            'Authorization: Digest username="reader", realm="search", nonce="digest-secret"': "Authorization: [redacted]",
            "X-Appbuilder-Authorization: OAuth oauth-secret-value": "X-Appbuilder-Authorization: [redacted]",
            'X_Appbuilder_Authorization = [{"token":"json-secret"}]': "X_Appbuilder_Authorization = [redacted]",
            "X-Api-Token: Bearer named-api-token": "X-Api-Token: [redacted]",
            "X-Token: Bearer named-token": "X-Token: [redacted]",
            "X-Secret: Basic named-secret": "X-Secret: [redacted]",
            "Cookie: Bearer named-cookie": "Cookie: [redacted]",
            "X-Secret: OAuth named-oauth": "X-Secret: [redacted]",
            "X-Token: Digest named-digest": "X-Token: [redacted]",
        }
        for source, expected in headers.items():
            with self.subTest(source=source):
                self.assertEqual(redact_sensitive_text(source), expected)

        url = (
            "https://reader:url-password@example.test/article?"
            "access%5Ftoken=encoded-token&X-Amz-Credential=amz-credential&"
            "X-Amz-Signature=amz-signature&signature=generic-signature&"
            "credential=generic-credential&%73%69%67=azure-signature&jwt=jwt-token&"
            "access_key=access-key&aws_access_key_id=aws-access-key-id&keep=public"
        )
        sanitized_url = redact_sensitive_text(url)
        for secret in (
            "url-password",
            "encoded-token",
            "amz-credential",
            "amz-signature",
            "generic-signature",
            "generic-credential",
            "azure-signature",
            "jwt-token",
            "access-key",
            "aws-access-key-id",
        ):
            self.assertNotIn(secret, sanitized_url)
        self.assertEqual(
            sanitized_url,
            "https://[redacted]@example.test/article?access%5Ftoken=[redacted]&"
            "X-Amz-Credential=[redacted]&X-Amz-Signature=[redacted]&"
            "signature=[redacted]&credential=[redacted]&%73%69%67=[redacted]&jwt=[redacted]&"
            "access_key=[redacted]&aws_access_key_id=[redacted]&keep=public",
        )
        self.assertEqual(
            redact_sensitive_text("https://reader:malformed-password@[bad/?sig=malformed-signature"),
            "https://[redacted]@[bad/?sig=[redacted]",
        )

        self.assertEqual(redact_sensitive_text("The bearer of good news arrived."), "The bearer of good news arrived.")
        self.assertEqual(redact_sensitive_text("Basic information is available."), "Basic information is available.")
        self.assertEqual(
            redact_sensitive_text("Bearer authentication is a guide."),
            "Bearer authentication is a guide.",
        )
        for source in (
            "Useful article text. Bearer content-secret should not leak.",
            "Useful article text. Bearer successful-payload-token should not leak.",
        ):
            with self.subTest(source=source):
                sanitized = redact_sensitive_text(source)
                self.assertNotIn("content-secret", sanitized)
                self.assertNotIn("successful-payload-token", sanitized)
                self.assertIn("Bearer [redacted]", sanitized)

    def test_url_fragments_redact_sensitive_pairs_and_preserve_benign_values(self) -> None:
        source = (
            "https://example.test/article#id_token=fragment-id&idtoken=compact-id&"
            "access_token=fragment-access&jwt=fragment-jwt&sig=fragment-sig&"
            "X-Amz-Signature=fragment-signature&token_value=fragment-value&tokenvalue=compact-value&"
            "section=overview&view=full"
        )
        expected = (
            "https://example.test/article#id_token=[redacted]&idtoken=[redacted]&"
            "access_token=[redacted]&jwt=[redacted]&sig=[redacted]&"
            "X-Amz-Signature=[redacted]&token_value=[redacted]&tokenvalue=[redacted]&"
            "section=overview&view=full"
        )

        self.assertEqual(redact_sensitive_text(source), expected)
        self.assertEqual(
            redact_sensitive_text(
                "https://example.test/article?id_token=query-id&token_value=query-value&keep=public"
            ),
            "https://example.test/article?id_token=[redacted]&token_value=[redacted]&keep=public",
        )
        self.assertEqual(
            redact_sensitive_text("https://reader:fragment-password@[bad/#id_token=malformed-token"),
            "https://[redacted]@[bad/#id_token=[redacted]",
        )

        result = SearchResult(
            title="fragment",
            url=source,
            metadata={"id_token": "mapping-id", "token_value": "mapping-value"},
        )
        result_data = result.to_dict()
        self.assertEqual(result_data["url"], expected)
        self.assertEqual(result_data["metadata"]["id_token"], "[redacted]")
        self.assertEqual(result_data["metadata"]["token_value"], "[redacted]")

        outcome = FetchOutcome(url=source)
        self.assertEqual(outcome.to_dict()["url"], expected)

    def test_plain_assignment_aliases_redact_through_six_encoded_key_layers(self) -> None:
        aliases = ("id_token", "idtoken", "token_value", "tokenvalue", "x_amz_signature", "set_cookie")

        def fully_percent_encode(value: str) -> str:
            return "".join(f"%{byte:02X}" for byte in value.encode("utf-8"))

        for alias in aliases:
            for layers in range(7):
                field = alias
                for _ in range(layers):
                    field = fully_percent_encode(field)
                secret = f"plain-assignment-{alias}-{layers}"
                source = f"{field}={secret}"
                with self.subTest(alias=alias, layers=layers):
                    self.assertEqual(redact_sensitive_text(source), f"{field}=[redacted]")
                    self.assertNotIn(secret, redact_sensitive_text(source))

                    result = SearchResult(
                        title="assignment",
                        url=f"https://example.test/article?{field}={secret}",
                        snippet=source,
                        metadata={field: secret},
                    )
                    result_data = result.to_dict()
                    self.assertEqual(result_data["url"], f"https://example.test/article?{field}=[redacted]")
                    self.assertEqual(result_data["snippet"], f"{field}=[redacted]")
                    self.assertEqual(result_data["metadata"][field], "[redacted]")

                    outcome = FetchOutcome(
                        url=f"https://example.test/article?{field}={secret}",
                        content={"format": "text", "text": source},
                        payload={"message": source},
                    )
                    outcome_data = outcome.to_dict()
                    rendered = str(outcome_data)
                    self.assertNotIn(secret, rendered)
                    self.assertEqual(outcome_data["url"], f"https://example.test/article?{field}=[redacted]")
                    self.assertEqual(outcome_data["content"]["text"], f"{field}=[redacted]")
                    self.assertEqual(outcome_data["payload"]["message"], f"{field}=[redacted]")

        ordinary = "Ordinary prose keeps name=value and discusses token values without an assignment."
        self.assertEqual(redact_sensitive_text(ordinary), ordinary)

    def test_url_sanitizer_redacts_nested_and_protocol_relative_urls(self) -> None:
        nested = (
            "https://gateway.test/redirect?next=https://reader:next-secret@target.test/path?"
            "access_token=nested-token"
        )
        nested_expected = (
            "https://gateway.test/redirect?next=https://[redacted]@target.test/path?"
            "access_token=[redacted]"
        )
        encoded = (
            "https://gateway.test/redirect?next=https%3A%2F%2Freader%3Aencoded-secret%40target.test%2Fpath%3F"
            "access_token%3Dencoded-token"
        )
        encoded_expected = (
            "https://gateway.test/redirect?next=https%3A%2F%2F%5Bredacted%5D%40target.test%2Fpath%3F"
            "access_token%3D%5Bredacted%5D"
        )
        encoded_value = (
            "https://reader:encoded-secret@target.test/path?access_token=encoded-token"
        )
        double_encoded_value = quote_plus(quote_plus(encoded_value))
        triple_encoded_value = quote_plus(double_encoded_value)
        double_encoded = f"https://gateway.test/redirect?next={double_encoded_value}"
        triple_fragment = f"https://gateway.test/#next={triple_encoded_value}"
        double_encoded_expected = (
            "https://gateway.test/redirect?next="
            f"{quote_plus(quote_plus('https://[redacted]@target.test/path?access_token=[redacted]'))}"
        )
        triple_fragment_expected = (
            "https://gateway.test/#next="
            f"{quote_plus(quote_plus(quote_plus('https://[redacted]@target.test/path?access_token=[redacted]')))}"
        )
        protocol_relative = "//reader:protocol-secret@target.test/path?token=protocol-token"
        protocol_expected = "//[redacted]@target.test/path?token=[redacted]"
        fragment = "https://gateway.test/#next=https://reader:fragment-secret@target.test/path"
        fragment_expected = "https://gateway.test/#next=https://[redacted]@target.test/path"
        malformed = "https://a@b:malformed-secret@[bad/path"
        malformed_expected = "https://[redacted]@[bad/path"
        nested_malformed = f"https://gateway.test/?next={malformed}"
        benign = "https://gateway.test/?next=https://public.test/path?view=full#section=overview"
        path_nested = (
            "https://gateway.test/path/https://reader:path-secret@target.test/article?"
            "token=path-token#id_token=path-id"
        )
        path_nested_expected = (
            "https://gateway.test/path/https://[redacted]@target.test/article?"
            "token=[redacted]#id_token=[redacted]"
        )
        path_encoded_inner = (
            "https://reader:path-encoded-secret@target.test/article?"
            "token=path-encoded-token#id_token=path-encoded-id"
        )
        path_encoded = f"https://gateway.test/path/{quote_plus(path_encoded_inner)}"
        path_encoded_expected = "https://gateway.test" + quote_plus(
            "/path/https://[redacted]@target.test/article?token=[redacted]#id_token=[redacted]",
            safe="/",
        )
        benign_path = "https://gateway.test/path/https://public.test/article?foo=public#section=overview"
        benign_encoded_path = f"https://gateway.test/path/{quote_plus('https://public.test/article?foo=public')}"

        self.assertEqual(redact_sensitive_text(nested), nested_expected)
        self.assertEqual(redact_sensitive_text(encoded), encoded_expected)
        self.assertEqual(redact_sensitive_text(double_encoded), double_encoded_expected)
        self.assertEqual(redact_sensitive_text(triple_fragment), triple_fragment_expected)
        self.assertEqual(redact_sensitive_text(protocol_relative), protocol_expected)
        self.assertEqual(
            redact_sensitive_text(f"https://gateway.test/?next={protocol_relative}"),
            f"https://gateway.test/?next={protocol_expected}",
        )
        self.assertEqual(redact_sensitive_text(fragment), fragment_expected)
        self.assertEqual(redact_sensitive_text(malformed), malformed_expected)
        self.assertEqual(
            redact_sensitive_text(nested_malformed),
            f"https://gateway.test/?next={malformed_expected}",
        )
        self.assertEqual(redact_sensitive_text(benign), benign)
        benign_encoded = f"https://gateway.test/?next={quote_plus('https://public.test/path?view=full')}"
        self.assertEqual(redact_sensitive_text(benign_encoded), benign_encoded)
        self.assertEqual(redact_sensitive_text(path_nested), path_nested_expected)
        self.assertEqual(redact_sensitive_text(path_encoded), path_encoded_expected)
        self.assertEqual(redact_sensitive_text(benign_path), benign_path)
        self.assertEqual(redact_sensitive_text(benign_encoded_path), benign_encoded_path)

        over_encoded_value = encoded_value
        for _ in range(6):
            over_encoded_value = quote_plus(over_encoded_value)
        over_encoded = f"https://gateway.test/?next={over_encoded_value}"
        over_sanitized = redact_sensitive_text(over_encoded)
        self.assertNotIn("encoded-secret", over_sanitized)
        self.assertNotIn("encoded-token", over_sanitized)
        self.assertIn("next=[redacted]", over_sanitized)

        self.assertEqual(SearchResult(title="nested", url=nested).to_dict()["url"], nested_expected)
        self.assertEqual(FetchOutcome(url=encoded).to_dict()["url"], encoded_expected)
        self.assertEqual(SearchResult(title="double", url=double_encoded).to_dict()["url"], double_encoded_expected)
        self.assertEqual(FetchOutcome(url=triple_fragment).to_dict()["url"], triple_fragment_expected)
        self.assertEqual(SearchResult(title="path", url=path_nested).to_dict()["url"], path_nested_expected)
        self.assertEqual(FetchOutcome(url=path_encoded).to_dict()["url"], path_encoded_expected)

    def test_deeply_nested_raw_and_encoded_urls_are_bounded_and_serializer_safe(self) -> None:
        raw_secret = "deep-raw-userinfo-secret"
        raw_token = "deep-raw-query-secret"
        raw = f"https://reader:{raw_secret}@target.test/article?access_token={raw_token}"
        # This deliberately exceeds ordinary recursion limits; public
        # serialization must stop at its bounded URL traversal budget.
        for _ in range(500):
            raw = f"https://gateway.test/redirect?next={raw}"

        encoded_secret = "deep-encoded-userinfo-secret"
        encoded_token = "deep-encoded-query-secret"
        encoded_inner = f"https://reader:{encoded_secret}@target.test/article?token={encoded_token}"
        encoded = encoded_inner
        for _ in range(12):
            encoded = quote_plus(encoded)
        encoded = f"https://gateway.test/redirect?next={encoded}"

        for source in (raw, encoded):
            with self.subTest(source_kind="raw" if source is raw else "encoded"):
                sanitized = redact_sensitive_text(source)
                self.assertNotIn(raw_secret, sanitized)
                self.assertNotIn(raw_token, sanitized)
                self.assertNotIn(encoded_secret, sanitized)
                self.assertNotIn(encoded_token, sanitized)
                self.assertEqual(SearchResult(title="deep", url=source).to_dict()["url"], sanitized)
                self.assertEqual(FetchOutcome(url=source).to_dict()["url"], sanitized)

    def test_public_serializers_are_cycle_safe_and_redact_standalone_assignments(self) -> None:
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        plan_cycle: dict[str, object] = {}
        plan_cycle["self"] = plan_cycle
        result = SearchResult(
            title="title",
            url="https://example.test/result",
            snippet="key=snippet-key",
            metadata={
                "nested": {"note": "key=nested-key"},
                "cookie": "cookie-value",
                "set-cookie": "set-cookie-value",
                "session_id": "session-value",
                "credentials": {"token": "credential-value"},
                "access_key": "access-key-value",
                "aws_access_key_id": "aws-access-key-id-value",
                "jwt": "jwt-value",
                "sig": "signature-value",
                "cycle": cycle,
            },
        )
        call = ProviderCall(
            provider="serper",
            query="key=query-key",
            results=[result],
            error="key=error-key",
            usage={"api_credentials": "api-credentials-value"},
        )
        response = SearchResponse(
            query="key=response-key",
            mode="web",
            plan=SearchPlan(mode="web", primary=["serper"], provider_options=plan_cycle),
            results=[result],
            calls=[call],
            elapsed_ms=0.0,
        )

        result_data = result.to_dict()
        call_data = call.to_dict()
        response_data = response.to_dict()
        rendered = str((result_data, call_data, response_data))
        for secret in (
            "snippet-key",
            "nested-key",
            "cookie-value",
            "set-cookie-value",
            "session-value",
            "credential-value",
            "access-key-value",
            "aws-access-key-id-value",
            "jwt-value",
            "signature-value",
            "query-key",
            "error-key",
            "api-credentials-value",
            "response-key",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(result_data["metadata"]["cookie"], "[redacted]")
        self.assertEqual(result_data["metadata"]["credentials"], "[redacted]")
        self.assertEqual(result_data["metadata"]["access_key"], "[redacted]")
        self.assertEqual(result_data["metadata"]["jwt"], "[redacted]")
        self.assertEqual(result_data["metadata"]["cycle"]["self"], "[cycle]")
        self.assertEqual(response_data["plan"]["provider_options"]["self"], "[cycle]")

    def test_public_response_serialization_sanitizes_nested_values(self) -> None:
        ordinary = "The client secret policy and private key format are documented."
        result = SearchResult(
            title='Bearer ["result-token"]',
            url="https://example.test/article?access_token=result-url-token",
            snippet="refresh_token=result-refresh-token",
            author="client_secret=result-client-secret",
            content="Authorization: (Bearer 'result-content-token')",
            metadata={
                "nested": ["Basic {result-basic-token}", ordinary],
                "client_key": "result-mapping-client-key",
            },
        )
        call = ProviderCall(
            provider="exa",
            query="auth_token=call-query-token",
            results=[result],
            status="error",
            error='X-Appbuilder-Authorization: [Bearer "call-error-token"]',
            usage={"note": "private_key=call-private-key", "private_key": "call-mapping-private-key"},
            metadata={"nested": {"url": "https://example.test/?client-key=call-client-key"}},
        )
        response = SearchResponse(
            query="api_token=response-query-token",
            mode="web",
            plan=SearchPlan(mode="web", primary=["exa"]),
            results=[result],
            calls=[call],
            elapsed_ms=1.0,
            warnings=["Authorization: [Bearer response-warning-token]"],
        )

        rendered = str((result.to_dict(), call.to_dict(), response.to_dict()))
        for secret in (
            "result-token",
            "result-url-token",
            "result-refresh-token",
            "result-client-secret",
            "result-content-token",
            "result-basic-token",
            "call-query-token",
            "call-error-token",
            "call-private-key",
            "call-mapping-private-key",
            "call-client-key",
            "response-query-token",
            "response-warning-token",
            "result-mapping-client-key",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn(ordinary, rendered)
        self.assertEqual(result.to_dict()["metadata"]["client_key"], "[redacted]")
        self.assertEqual(call.to_dict()["usage"]["private_key"], "[redacted]")
        self.assertIn("results", response.to_dict())
        self.assertIn("providers", response.to_dict())

    def test_timestamp_signals_are_descriptive_and_relative_text_is_unknown(self) -> None:
        result = SearchResult(
            title="Title",
            url="https://www.example.test/article",
            snippet="Snippet",
            author="Author",
            content="Body",
            provider="serper+brave",
            published_at="Mon, 10 Aug 2026 00:00:00 GMT",
            metadata={"matched_providers": ["serper", "brave"]},
        )
        signals = build_evidence_signals(
            result,
            ["serper", "brave", "tavily"],
            reference_time=datetime(2026, 8, 11, tzinfo=UTC),
        )

        self.assertEqual(parse_standard_timestamp(result.published_at), datetime(2026, 8, 10, tzinfo=UTC))
        self.assertEqual(signals["verification_status"], "not_verified")
        self.assertEqual(signals["matched_provider_count"], 2)
        self.assertEqual(signals["participating_successful_provider_count"], 3)
        self.assertAlmostEqual(signals["provider_agreement_ratio"], 2 / 3, places=4)
        self.assertEqual(signals["source_domain"], "example.test")
        self.assertTrue(signals["title_present"])
        self.assertEqual(signals["author_length"], len("Author"))
        self.assertEqual(signals["published_at_raw"], result.published_at)
        self.assertEqual(signals["published_at_parse_status"], "parsed")
        self.assertEqual(signals["published_at_normalized"], "2026-08-10T00:00:00+00:00")
        self.assertEqual(signals["published_at_age_seconds"], 86400.0)
        self.assertTrue({"truth_score", "official", "primary", "source_independence"}.isdisjoint(signals))

        result.published_at = "two days ago"
        unknown = build_evidence_signals(
            result,
            ["serper"],
            reference_time=datetime(2026, 8, 11, tzinfo=UTC),
        )
        self.assertEqual(unknown["published_at_parse_status"], "unknown")
        self.assertIsNone(unknown["published_at_normalized"])
        self.assertIsNone(unknown["published_at_age_seconds"])

    def test_search_annotates_only_final_fused_results(self) -> None:
        plan = SearchPlan(mode="web", primary=["serper", "brave"], fallback=[], min_results=1)

        class FakeRouter:
            def plan(self, *args: object, **kwargs: object) -> SearchPlan:
                return plan

            def weights_for(self, mode: str) -> dict[str, float]:
                return {}

        raw = SearchResult(
            "raw",
            "https://example.test/article?utm_source=test",
            provider="serper",
            rank=1,
            published_at="2026-08-10T00:00:00Z",
        )
        matching = SearchResult(
            "matching",
            "https://www.example.test/article",
            provider="brave",
            rank=1,
        )
        calls = [
            ProviderCall(provider="serper", query="query", results=[raw]),
            ProviderCall(provider="brave", query="query", results=[matching]),
        ]
        engine = SearchEngine.__new__(SearchEngine)
        engine.settings = Settings(result_limit=5)
        engine.router = FakeRouter()
        engine._run_many = lambda *args, **kwargs: calls  # type: ignore[method-assign]
        engine.configured_providers = lambda: ["serper", "brave"]  # type: ignore[method-assign]

        response = engine.search("query")

        self.assertEqual(len(response.results), 1)
        evidence = response.results[0].metadata["evidence"]
        self.assertEqual(evidence["matched_provider_count"], 2)
        self.assertEqual(evidence["participating_successful_provider_count"], 2)
        self.assertNotIn("truth_score", evidence)
        self.assertNotIn("evidence", raw.metadata)
        self.assertNotIn("evidence", matching.metadata)


class DetailedFetchTests(unittest.TestCase):
    def test_fetch_rejects_blank_url_before_provider_access(self) -> None:
        class NoProviderAccess(dict[str, object]):
            def get(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("blank URL must not inspect provider configuration")

        engine = SearchEngine.__new__(SearchEngine)
        engine.providers = NoProviderAccess()

        with self.assertRaisesRegex(ValueError, "url must be a non-empty string"):
            engine.fetch(" \t\n")

    def test_engine_caught_provider_errors_use_shared_sanitizer(self) -> None:
        class FailingProvider:
            configured = True

            def search(self, query: str, **kwargs: object) -> ProviderCall:
                raise RuntimeError('Authorization: [Bearer "engine-error-token"]')

        engine = SearchEngine.__new__(SearchEngine)
        engine.settings = Settings(max_workers=1)
        engine.providers = {"broken": FailingProvider()}

        calls = engine._run_many(
            ["broken"],
            "query?access_token=engine-query-token",
            {},
            1,
        )

        rendered = str(calls[0].to_dict())
        self.assertNotIn("engine-error-token", rendered)
        self.assertNotIn("engine-query-token", rendered)
        self.assertEqual(calls[0].error, "Authorization: [redacted]")

    def test_legacy_fetch_auto_falls_through_failed_firecrawl_to_tavily(self) -> None:
        failed_payload = {"ok": False, "error": "firecrawl unavailable"}
        successful_payload = {"provider": "tavily", "results": [{"raw_content": "Tavily content"}]}

        class FailedFirecrawl:
            configured = True

            def scrape(self, url: str) -> dict[str, object]:
                return failed_payload

        class WorkingTavily:
            configured = True

            def extract(self, urls: list[str]) -> dict[str, object]:
                return successful_payload

        engine = SearchEngine.__new__(SearchEngine)
        engine.providers = {"firecrawl": FailedFirecrawl(), "tavily": WorkingTavily()}

        auto = engine.fetch("https://example.test/article")
        explicit = engine.fetch("https://example.test/article", provider="firecrawl")

        self.assertEqual(auto, successful_payload)
        self.assertEqual(explicit, failed_payload)

    def test_legacy_fetch_sanitizes_success_and_failure_library_outputs(self) -> None:
        class WorkingFirecrawl:
            configured = True

            def scrape(self, url: str) -> dict[str, object]:
                return {
                    "data": {"markdown": "Useful text. Bearer successful-payload-token"},
                    "url": "https://example.test/page?access_token=successful-url-token&x=1",
                    "message": "X-Appbuilder-Authorization: [Bearer appbuilder-success-token]",
                }

        class FailingFirecrawl:
            configured = True

            def scrape(self, url: str) -> dict[str, object]:
                return {
                    "ok": False,
                    "error": "Authorization: [Bearer explicit-failure-token]",
                    "url": "https://example.test/page?private_key=explicit-private-key",
                }

        class RaisingFirecrawl:
            configured = True

            def scrape(self, url: str) -> dict[str, object]:
                raise RuntimeError("Authorization: [Bearer exception-token]")

        engine = SearchEngine.__new__(SearchEngine)
        engine.providers = {"firecrawl": WorkingFirecrawl()}
        successful = engine.fetch("https://example.test/article")

        engine.providers = {"firecrawl": FailingFirecrawl()}
        explicit_failure = engine.fetch("https://example.test/article", provider="firecrawl")

        engine.providers = {"firecrawl": RaisingFirecrawl()}
        final_failure = engine.fetch("https://example.test/article?refresh_token=final-url-token")

        rendered = str((successful, explicit_failure, final_failure))
        for secret in (
            "successful-payload-token",
            "successful-url-token",
            "appbuilder-success-token",
            "explicit-failure-token",
            "explicit-private-key",
            "exception-token",
            "final-url-token",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(successful["url"], "https://example.test/page?access_token=[redacted]&x=1")
        self.assertEqual(explicit_failure["error"], "Authorization: [redacted]")
        self.assertEqual(final_failure["url"], "https://example.test/article?refresh_token=[redacted]")
        self.assertEqual(final_failure["errors"][0]["error"], "Authorization: [redacted]")

    def test_legacy_fetch_records_invalid_payloads_and_keeps_final_error_envelope(self) -> None:
        class NoneFirecrawl:
            configured = True

            def scrape(self, url: str) -> None:
                return None

        class HttpErrorTavily:
            configured = True

            def extract(self, urls: list[str]) -> dict[str, object]:
                return {"status_code": 503, "results": [{"raw_content": "must not return"}]}

        class EmptyExa:
            configured = True

            def contents(self, urls: list[str]) -> dict[str, object]:
                return {"results": []}

        engine = SearchEngine.__new__(SearchEngine)
        engine.providers = {
            "firecrawl": NoneFirecrawl(),
            "tavily": HttpErrorTavily(),
            "exa": EmptyExa(),
        }

        result = engine.fetch("https://example.test/article")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["url"], "https://example.test/article")
        self.assertEqual(result["error"], "no fetch provider succeeded")
        self.assertEqual([entry["provider"] for entry in result["errors"]], ["firecrawl", "tavily", "exa"])

    def test_fetch_detailed_tracks_order_and_explicit_failure_payloads_safely(self) -> None:
        class DisabledFirecrawl:
            configured = False

        class FailedTavily:
            configured = True

            def extract(self, urls: list[str]) -> dict[str, object]:
                return {
                    "ok": False,
                    "elapsed_ms": 2.0,
                    "http_status": 503,
                    "error": "temporary failure",
                    "headers": {"Authorization": "Bearer hidden"},
                }

        class WorkingExa:
            configured = True

            def contents(self, urls: list[str]) -> dict[str, object]:
                return {
                    "success": True,
                    "elapsed_ms": 3.0,
                    "results": [{"text": "Fetched article text"}],
                    "headers": {"X-API-KEY": "hidden"},
                }

        engine = SearchEngine.__new__(SearchEngine)
        engine.providers = {
            "firecrawl": DisabledFirecrawl(),
            "tavily": FailedTavily(),
            "exa": WorkingExa(),
        }

        outcome = engine.fetch_detailed("https://example.test/article")
        data = outcome.to_dict()

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.selected_provider, "exa")
        self.assertEqual([attempt.provider for attempt in outcome.attempts], ["firecrawl", "tavily", "exa"])
        self.assertEqual([attempt.status for attempt in outcome.attempts], ["unconfigured", "error", "ok"])
        self.assertEqual(outcome.attempts[1].http_status, 503)
        self.assertEqual(data["content"], {"format": "text", "text": "Fetched article text"})
        self.assertNotIn("headers", data["payload"])
        self.assertNotIn("hidden", str(data))

    def test_fetch_outcome_serialization_redacts_inline_credentials(self) -> None:
        outcome = FetchOutcome(
            url="https://example.test/article",
            attempts=[
                FetchAttempt(
                    provider="tavily",
                    status="error",
                    error="Authorization: Bearer attempt-secret token=attempt-token",
                )
            ],
            selected_provider="exa",
            content={
                "format": "text",
                "text": "Useful article text. Bearer content-secret API-key=content-key.",
            },
            payload={
                "message": "token=payload-token; useful provider message",
                "nested": ["Authorization: Bearer nested-secret", 'x-api-key: "quoted-key"', "ordinary text"],
                "compound": (
                    "access_token=access-value&refresh-token=refresh-value "
                    "client_secret: client-secret client-key='client-key-value' private_key=private-key-value"
                ),
                "client_key": "removed-by-key",
                "headers": {"Authorization": "Bearer removed-by-key"},
                "cookie": "removed-cookie",
                "set-cookie": "removed-set-cookie",
                "session_token": "removed-session-token",
                "credentials": {"token": "removed-credential"},
            },
        )

        data = outcome.to_dict()
        rendered = str(data)

        for secret in (
            "attempt-secret",
            "attempt-token",
            "content-secret",
            "content-key",
            "payload-token",
            "nested-secret",
            "quoted-key",
            "access-value",
            "refresh-value",
            "client-secret",
            "client-key-value",
            "private-key-value",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("Useful article text.", data["content"]["text"])
        self.assertIn("ordinary text", data["payload"]["nested"])
        for key in ("headers", "client_key", "cookie", "set-cookie", "session_token", "credentials"):
            self.assertNotIn(key, data["payload"])

    def test_fetch_outcome_sanitizes_urls_nested_bearer_values_and_wrappers(self) -> None:
        outcome = FetchOutcome(
            url="https://example.test/article?access_token=outcome-access&client-key=outcome-client-key",
            attempts=[
                FetchAttempt(
                    provider="firecrawl",
                    status="error",
                    error="Authorization: [Bearer outcome-attempt-token]",
                )
            ],
            selected_provider="exa",
            content={"format": "text", "text": "X-Appbuilder-Authorization: Bearer outcome-content-token"},
            payload={
                "nested": [
                    "Bearer outcome-list-token",
                    "Basic {outcome-basic-token}",
                    "Authorization: (Bearer outcome-parenthesized-token)",
                ],
                "url": "https://example.test/path?refresh_token=outcome-refresh&private_key=outcome-private",
            },
        )

        data = outcome.to_dict()
        rendered = str(data)

        for secret in (
            "outcome-access",
            "outcome-client-key",
            "outcome-attempt-token",
            "outcome-content-token",
            "outcome-list-token",
            "outcome-basic-token",
            "outcome-parenthesized-token",
            "outcome-refresh",
            "outcome-private",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(data["url"], "https://example.test/article?access_token=[redacted]&client-key=[redacted]")
        self.assertEqual(data["attempts"][0]["error"], "Authorization: [redacted]")
        self.assertEqual(data["payload"]["nested"][2], "Authorization: (redacted)")
        self.assertNotIn("]]]", data["attempts"][0]["error"])

    def test_fetch_outcome_preserves_ordinary_compound_name_prose(self) -> None:
        ordinary = "The client secret policy and private key format are documented."
        data = FetchOutcome(
            url="https://example.test/article",
            selected_provider="exa",
            content={"format": "text", "text": ordinary},
            payload={"description": ordinary},
        ).to_dict()

        self.assertEqual(data["content"]["text"], ordinary)
        self.assertEqual(data["payload"]["description"], ordinary)

    def test_fetch_serializers_redact_untrusted_provider_labels(self) -> None:
        attempt = FetchAttempt(provider="authorization=attempt-provider-secret", status="ok")
        outcome = FetchOutcome(
            url="https://reader:outcome-password@example.test/page?sig=outcome-signature",
            attempts=[attempt],
            selected_provider="authorization=outcome-provider-secret",
            payload={"jwt": "payload-jwt", "access_key": "payload-access-key"},
        ).to_dict()
        rendered = str((attempt.to_dict(), outcome))

        for secret in (
            "attempt-provider-secret",
            "outcome-password",
            "outcome-signature",
            "outcome-provider-secret",
            "payload-jwt",
            "payload-access-key",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(outcome["attempts"][0]["provider"], "authorization=[redacted]")
        self.assertEqual(outcome["selected_provider"], "authorization=[redacted]")
        self.assertNotIn("jwt", outcome["payload"])
        self.assertNotIn("access_key", outcome["payload"])

    def test_fetch_detailed_rejects_malformed_and_http_error_payloads_before_fallback(self) -> None:
        class NoneFirecrawl:
            configured = True

            def scrape(self, url: str) -> None:
                return None

        class HttpErrorTavily:
            configured = True

            def extract(self, urls: list[str]) -> dict[str, object]:
                return {"status_code": 502, "results": [{"text": "must not be selected"}]}

        class WorkingExa:
            configured = True

            def contents(self, urls: list[str]) -> dict[str, object]:
                return {"results": [{"text": "usable fallback content"}]}

        engine = SearchEngine.__new__(SearchEngine)
        engine.providers = {
            "firecrawl": NoneFirecrawl(),
            "tavily": HttpErrorTavily(),
            "exa": WorkingExa(),
        }

        outcome = engine.fetch_detailed("https://example.test/article")

        self.assertEqual(outcome.selected_provider, "exa")
        self.assertEqual([attempt.status for attempt in outcome.attempts], ["error", "error", "ok"])
        self.assertEqual(outcome.attempts[0].error, "provider returned no usable fetch payload")
        self.assertEqual(outcome.attempts[1].http_status, 502)
        self.assertEqual(outcome.content["text"], "usable fallback content")

        class EmptyFirecrawl:
            configured = True

            def scrape(self, url: str) -> dict[str, object]:
                return {"data": {}}

        engine.providers = {"firecrawl": EmptyFirecrawl()}
        empty = engine.fetch_detailed("https://example.test/article", provider="firecrawl")
        self.assertFalse(empty.ok)
        self.assertEqual(empty.attempts[0].status, "error")
        self.assertEqual(empty.attempts[0].error, "provider returned no extractable fetch content")


class DeterministicHelperTests(unittest.TestCase):
    def test_collect_evidence_fetches_original_url_but_sanitizes_its_envelope(self) -> None:
        engine = SearchEngine.__new__(SearchEngine)
        fetched: list[str] = []

        def fake_search(query: str, **kwargs: object) -> SearchResponse:
            return _response(query, results=[])

        def fake_fetch(url: str, provider: str = "auto") -> FetchOutcome:
            fetched.append(url)
            return FetchOutcome(
                url=url,
                attempts=[FetchAttempt(provider="exa", status="ok")],
                selected_provider="exa",
                content={"format": "text", "text": "content"},
            )

        engine.search = fake_search  # type: ignore[method-assign]
        engine.fetch_detailed = fake_fetch  # type: ignore[method-assign]
        original = "https://www.example.test/article?access_token=evidence-url-token&keep=1"

        envelope = engine.collect_evidence("query", urls=[original])

        self.assertEqual(fetched, [original])
        item = envelope["evidence"][0]
        self.assertEqual(item["url"], "https://www.example.test/article?access_token=[redacted]&keep=1")
        self.assertEqual(item["canonical_url"], "https://example.test/article?access_token=[redacted]&keep=1")

    def test_multi_search_is_sequential_and_validates_queries(self) -> None:
        engine = SearchEngine.__new__(SearchEngine)
        seen: list[str] = []

        def fake_search(query: str, **kwargs: object) -> SearchResponse:
            seen.append(query)
            return _response(query)

        engine.search = fake_search  # type: ignore[method-assign]
        responses = engine.multi_search(["first", "second"], mode="web", all_fallbacks=True)

        self.assertEqual(seen, ["first", "second"])
        self.assertEqual([response.query for response in responses], ["first", "second"])
        with self.assertRaisesRegex(ValueError, "non-empty"):
            engine.multi_search(["valid", ""])
        self.assertEqual(seen, ["first", "second"])

    def test_research_plan_is_offline_and_collect_evidence_selects_urls_deterministically(self) -> None:
        plan = SearchPlan(mode="web", primary=["serper"], fallback=["brave"], min_results=2)

        class FakeRouter:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, object, object]] = []

            def plan(self, query: str, mode: str, *, freshness: object = None, domains: object = None) -> SearchPlan:
                self.calls.append((query, mode, freshness, domains))
                return plan

        engine = SearchEngine.__new__(SearchEngine)
        engine.router = FakeRouter()
        first = SearchResult("first", "https://example.test/a?utm_source=test", provider="serper", rank=1)
        second = SearchResult(
            "second",
            "https://example.test/b?keep=1&signature=signed-value",
            provider="serper",
            rank=2,
        )
        search_calls: list[str] = []
        fetched: list[str] = []

        def fake_search(query: str, **kwargs: object) -> SearchResponse:
            search_calls.append(query)
            return _response(query, results=[first, second])

        def fake_fetch(url: str, provider: str = "auto") -> FetchOutcome:
            fetched.append(url)
            return FetchOutcome(
                url=url,
                attempts=[FetchAttempt(provider="firecrawl", status="ok")],
                selected_provider="firecrawl",
                content={"format": "markdown", "text": "content"},
                payload={"data": {"markdown": "content"}},
            )

        engine.search = fake_search  # type: ignore[method-assign]
        engine.fetch_detailed = fake_fetch  # type: ignore[method-assign]

        research = engine.research_plan("research query", mode="web")
        self.assertEqual([step["step"] for step in research["workflow"]], ["search", "evidence-gap-check", "optional-fetch", "cross-check"])
        self.assertEqual(research["stop_criteria"]["max_optional_fetches"], 3)
        self.assertEqual(engine.router.calls, [("research query", "web", None, None)])

        explicit = engine.collect_evidence(
            "evidence query",
            urls=[
                "https://www.example.test/b?signature=signed-value&keep=1",
                "https://example.test/b?keep=1&signature=signed-value",
            ],
            fetch_limit=2,
        )
        self.assertEqual(search_calls, ["evidence query"])
        self.assertEqual(fetched, ["https://www.example.test/b?signature=signed-value&keep=1"])
        self.assertEqual(
            explicit["evidence"][0]["url"],
            "https://www.example.test/b?signature=[redacted]&keep=1",
        )
        self.assertEqual(
            explicit["evidence"][0]["canonical_url"],
            "https://example.test/b?keep=1&signature=[redacted]",
        )
        self.assertEqual(explicit["evidence"][0]["result"]["title"], "second")
        self.assertEqual(explicit["counts"]["selected_urls"], 1)

        fetched.clear()
        ranked = engine.collect_evidence("ranked query", fetch_limit=2)
        self.assertEqual(
            fetched,
            ["https://example.test/a?utm_source=test", "https://example.test/b?keep=1&signature=signed-value"],
        )
        self.assertEqual(
            [item["url"] for item in ranked["evidence"]],
            [
                "https://example.test/a?utm_source=test",
                "https://example.test/b?keep=1&signature=[redacted]",
            ],
        )


if __name__ == "__main__":
    unittest.main()
