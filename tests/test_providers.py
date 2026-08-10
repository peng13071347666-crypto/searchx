from __future__ import annotations

import unittest
from unittest.mock import patch

from searchx.http import HttpResult
from searchx.providers import SerperProvider


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request_json(self, method: str, url: str, **kwargs: object) -> HttpResult:
        self.calls.append({"method": method, "url": url, **kwargs})
        return HttpResult(
            status=200,
            elapsed_ms=4.0,
            headers={},
            data={
                "organic": [
                    {
                        "link": "https://example.test/string-info",
                        "title": "String publication info",
                        "snippet": "A safe scholar result",
                        "year": "2025",
                        "publicationInfo": "Journal of Safe Tests",
                    },
                    {
                        "link": "https://example.test/mapping-info",
                        "title": "Mapping publication info",
                        "snippet": "Another safe result",
                        "year": "2024",
                        "publicationInfo": {"summary": "Mapped journal summary"},
                    },
                    {
                        "link": "https://example.test/malformed-info",
                        "title": "Malformed publication info",
                        "snippet": "Still usable",
                        "year": "2023",
                        "publicationInfo": ["not", "a", "mapping"],
                    },
                    None,
                ]
            },
        )


class SerperProviderTests(unittest.TestCase):
    def test_scholar_normalizes_string_and_mapping_publication_info(self) -> None:
        http = _FakeHttp()
        provider = SerperProvider(http)  # type: ignore[arg-type]

        with patch("searchx.providers.api_key", return_value="test-only-key"):
            call = provider.search("safe scholar query", mode="academic")

        self.assertTrue(call.ok)
        self.assertEqual(call.metadata, {"endpoint": "scholar"})
        self.assertEqual([result.author for result in call.results], ["Journal of Safe Tests", "Mapped journal summary", None])
        self.assertTrue(all(result.metadata == {"endpoint": "scholar"} for result in call.results))
        self.assertEqual(http.calls[0]["url"], "https://google.serper.dev/scholar")


if __name__ == "__main__":
    unittest.main()
