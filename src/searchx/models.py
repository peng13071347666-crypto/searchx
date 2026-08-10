from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import re
from typing import Any
from urllib.parse import quote_plus, unquote_plus, urlsplit, urlunsplit


_WRAPPER_PAIRS = {"[": "]", "{": "}", "(": ")"}
_CYCLE_MARKER = "[cycle]"
_REDACTION_SENTINEL = "\x00searchx-redacted\x00"
_MAX_NESTED_URL_DECODE_DEPTH = 4
# This deliberately remains modest: it is a safety boundary for arbitrary
# provider text, not a promise to recursively interpret an unbounded redirect
# chain.  Values below this depth retain their useful non-sensitive parts.
_MAX_NESTED_URL_RECURSION_DEPTH = 16
_SENSITIVE_FIELD_EXACT = {
    "apikey",
    "apitoken",
    "authorization",
    "authentication",
    "auth",
    "bearer",
    "basic",
    "token",
    "secret",
    "password",
    "key",
    "accesstoken",
    "authtoken",
    "refreshtoken",
    "idtoken",
    "tokenvalue",
    "clientsecret",
    "clientkey",
    "privatekey",
    "credential",
    "credentials",
    "cookie",
    "setcookie",
    "session",
    "sessionid",
    "sessiontoken",
    "sessionkey",
    "signature",
    "sig",
    "jwt",
    "accesskey",
    "accesskeyid",
    "awsaccesskeyid",
    "awssecretaccesskey",
    "xamzcredential",
    "xamzsignature",
    "sid",
    "header",
    "headers",
}
_SENSITIVE_FIELD_SUFFIXES = (
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "authentication",
    "credential",
    "credentials",
    "cookie",
    "session",
    "sessionid",
    "sessiontoken",
    "clientkey",
    "privatekey",
    "accesskey",
    "accesskeyid",
    "signature",
    "header",
    "headers",
)


def _normalized_name_details(value: object) -> tuple[str, bool]:
    """Normalize a possibly encoded field name within the shared decode budget."""
    text = str(value)
    for _ in range(_MAX_NESTED_URL_DECODE_DEPTH):
        decoded = unquote_plus(text)
        if decoded == text:
            return "".join(character for character in text.lower() if character.isalnum()), False
        text = decoded
    unresolved = unquote_plus(text) != text
    return "".join(character for character in text.lower() if character.isalnum()), unresolved


def _normalized_name(value: object) -> str:
    return _normalized_name_details(value)[0]


def _sensitive_fetch_field(name: object) -> bool:
    normalized, unresolved = _normalized_name_details(name)
    return unresolved or normalized in _SENSITIVE_FIELD_EXACT or normalized.endswith(_SENSITIVE_FIELD_SUFFIXES)


def _sensitive_query_name(name: object) -> bool:
    return _sensitive_fetch_field(name)


_URL_RE = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*:|(?<![a-z0-9+.-]))//[^\s<>\"']+")
_QUERY_PAIR_RE = re.compile(r"(?P<prefix>[?#&])(?P<name>[^=&\s]+)=(?P<value>[^&#\s]*)")
_AUTHORIZATION_VALUE_RE = re.compile(
    r"""(?ix)
    (?P<prefix>\b(?:x[-_])?(?:appbuilder[-_])?authorization\b[\"']?\s*[:=]\s*)
    (?P<value>[^\r\n]*)
    """
)
_BARE_CREDENTIAL_RE = re.compile(
    r"""(?ix)
    \b(?P<scheme>bearer|basic)\s+
    (?P<value>
        "[^"\r\n]*"|'[^'\r\n]*'
        |\[[^\]\r\n]*\]|\{[^\}\r\n]*\}|\([^\)\r\n]*\)
        |[^\s,;\"'\]\}\)&]+
    )
    """
)


def _unwrap_credential_value(value: str) -> tuple[str, str, str, str]:
    """Return quote/open/close/core while retaining only balanced wrappers."""
    quote = ""
    core = value
    if len(core) >= 2 and core[0] in "\"'" and core[-1] == core[0]:
        quote, core = core[0], core[1:-1]
    opening = ""
    closing = ""
    if len(core) >= 2 and core[0] in _WRAPPER_PAIRS and core[-1] == _WRAPPER_PAIRS[core[0]]:
        opening, closing, core = core[0], core[-1], core[1:-1]
    return quote, opening, closing, core


def _redacted_complete_value(value: str) -> str:
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()) :]
    quote, opening, closing, _ = _unwrap_credential_value(value.strip())
    if opening:
        marker = "redacted"
        return f"{leading}{quote}{opening}{marker}{closing}{quote}{trailing}"
    return f"{leading}{quote}[redacted]{quote}{trailing}"


def _redacted_bare_value(scheme: str, value: str, *, outer_opening: str = "") -> str:
    raw = value
    trailing = ""
    expected_closing = _WRAPPER_PAIRS.get(outer_opening)
    if expected_closing and raw.endswith(expected_closing):
        raw, trailing = raw[:-1], expected_closing
    quote, opening, closing, core = _unwrap_credential_value(raw)
    token_quote = quote
    if opening and len(core) >= 2 and core[0] in "\"'" and core[-1] == core[0]:
        token_quote = core[0]
    if opening:
        marker = f"{token_quote}redacted{token_quote}" if token_quote else "redacted"
        return f"{scheme.title()} {opening}{marker}{closing}{trailing}"
    marker = f"{token_quote}[redacted]{token_quote}" if token_quote else "[redacted]"
    return f"{scheme.title()} {marker}{trailing}"


def _looks_like_bare_credential(value: str) -> bool:
    """Recognize token-shaped bare scheme values without treating prose as a token.

    ``Bearer authentication`` and ``Basic information`` occur in ordinary prose.
    A real credential is usually quoted/wrapped, has a token separator or digit,
    is all-caps, or calls itself a token/secret/key.  Those shapes are safe to
    redact regardless of the surrounding sentence.
    """
    if any(character in value for character in "\"'[]{}()"):
        return True
    _, _, _, core = _unwrap_credential_value(value)
    normalized = core.lower()
    return (
        bool(re.search(r"[0-9._~+/:=@%\-]", core))
        or (len(core) > 1 and core.isupper())
        or bool(re.search(r"(?:token|secret|credential|apikey|accesskey|jwt)", normalized))
    )


def _is_bare_credential_context(match: re.Match[str]) -> bool:
    value = match.group("value")
    if _looks_like_bare_credential(value):
        return True
    preceding = match.string[: match.start()].rstrip()
    # A labelled/header-like value is credential context even when the value
    # itself is word-like.  At the start of a prose sentence, leave ordinary
    # words alone unless they have one of the token shapes above.
    return bool(preceding and preceding[-1] in ":=;,([{")


def _bare_outer_opening(match: re.Match[str]) -> str:
    preceding = match.string[: match.start()].rstrip()
    return preceding[-1] if preceding and preceding[-1] in _WRAPPER_PAIRS else ""


def _sanitize_query(query: str, *, nested_depth: int = 0) -> str:
    if not query:
        return query
    parts: list[str] = []
    for part in query.split("&"):
        name, separator, value = part.partition("=")
        if not separator:
            parts.append(part)
        elif _sensitive_query_name(name):
            parts.append(f"{name}=[redacted]")
        else:
            parts.append(
                f"{name}={_sanitize_nested_url_value(value, nested_depth=nested_depth + 1)}"
            )
    return "&".join(parts)


def _sanitize_nested_url_value(
    value: str,
    *,
    reencode_safe: str = "",
    nested_depth: int = 0,
) -> str:
    """Redact a URL carried in a non-sensitive query or fragment value.

    Preserve values verbatim unless they actually need redaction.  Encoded
    values are decoded only for inspection, then encoded again only after the
    decoded URL was changed.
    """
    if nested_depth >= _MAX_NESTED_URL_RECURSION_DEPTH:
        # A chain beyond the structural inspection budget can conceal another
        # credential-bearing URL.  Returning a marker is safer than walking
        # an attacker-controlled number of nested redirects.
        return "[redacted]"

    candidate = value
    for decode_depth in range(_MAX_NESTED_URL_DECODE_DEPTH + 1):
        sanitized = _sanitize_url(candidate, nested_depth=nested_depth)
        if sanitized == candidate:
            sanitized = _URL_RE.sub(
                lambda match: _sanitize_url(match.group(0), nested_depth=nested_depth),
                candidate,
            )
        if sanitized != candidate:
            # Restore precisely the number of URL-encoding layers stripped for
            # inspection, keeping the surrounding outer URL structurally valid.
            for _ in range(decode_depth):
                sanitized = quote_plus(sanitized, safe=reencode_safe)
            return sanitized

        decoded = unquote_plus(candidate)
        if decoded == candidate:
            return value
        if decode_depth == _MAX_NESTED_URL_DECODE_DEPTH:
            # An excessively nested value could conceal a URL past our bounded
            # inspection budget.  Redact conservatively rather than return a
            # credential-bearing value unchanged.
            return "[redacted]"
        candidate = decoded

    return "[redacted]"


def _sanitize_url(value: str, *, nested_depth: int = 0) -> str:
    if nested_depth >= _MAX_NESTED_URL_RECURSION_DEPTH:
        return "[redacted]"
    try:
        parts = urlsplit(value)
    except ValueError:
        # Keep malformed URLs usable enough for diagnostics, but never leave
        # literal userinfo behind merely because the host cannot be parsed.
        return re.sub(
            r"(?i)(?P<prefix>(?:[a-z][a-z0-9+.-]*:)?//)[^/?#\s]*@",
            lambda match: f"{match.group('prefix')}[redacted]@",
            value,
            count=1,
        )
    if not parts.netloc:
        return value
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "[redacted]@" + netloc.rsplit("@", 1)[1]
    path = _sanitize_nested_url_value(
        parts.path,
        reencode_safe="/",
        nested_depth=nested_depth + 1,
    )
    query = _sanitize_query(parts.query, nested_depth=nested_depth)
    fragment = _sanitize_query(parts.fragment, nested_depth=nested_depth)
    if path == parts.path and netloc == parts.netloc and query == parts.query and fragment == parts.fragment:
        return value
    return urlunsplit((parts.scheme, netloc, path, query, fragment))


_TEXT_FIELD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_%+-.")
_TEXT_FIELD_BOUNDARIES = frozenset(" \t\r\n?&#=;,:([{\"'")


def _text_assignment_name(value: str, separator: int) -> str | None:
    """Extract a field-style assignment label without mistaking URL paths for one."""
    end = separator
    while end > 0 and value[end - 1].isspace():
        end -= 1
    if end > 0 and value[end - 1] in "\"'":
        end -= 1
    name_end = end
    start = end
    while start > 0 and value[start - 1] in _TEXT_FIELD_CHARS:
        start -= 1
    if start > 0 and value[start - 1] not in _TEXT_FIELD_BOUNDARIES:
        return None
    name = value[start:name_end].strip()
    return name or None


def _redact_nonheader_text(value: str) -> str:
    """Redact field assignments through the same classifier used by maps and URLs."""
    chunks: list[str] = []
    cursor = 0
    index = 0
    while index < len(value):
        if value[index] not in ":=":
            index += 1
            continue
        name = _text_assignment_name(value, index)
        if name is None or not _sensitive_fetch_field(name):
            index += 1
            continue
        value_start = index + 1
        while value_start < len(value) and value[value_start].isspace():
            value_start += 1
        if value.startswith(_REDACTION_SENTINEL, value_start):
            index = value_start + len(_REDACTION_SENTINEL)
            continue
        line_end = value.find("\n", value_start)
        if line_end < 0:
            line_end = len(value)
        chunks.append(value[cursor:value_start])
        chunks.append(_redacted_complete_value(value[value_start:line_end]))
        cursor = line_end
        index = line_end
    chunks.append(value[cursor:])
    return _BARE_CREDENTIAL_RE.sub(
        lambda match: _redacted_bare_value(
            match.group("scheme"), match.group("value"), outer_opening=_bare_outer_opening(match)
        )
        if _is_bare_credential_context(match)
        else match.group(0),
        "".join(chunks),
    )


def redact_sensitive_text(value: str) -> str:
    """Redact credential-shaped text while retaining normal prose and usable URLs."""
    text = value.replace("[redacted]", _REDACTION_SENTINEL)
    text = _URL_RE.sub(lambda match: _sanitize_url(match.group(0)), text)
    text = _QUERY_PAIR_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('name')}=[redacted]"
        if _sensitive_query_name(match.group("name"))
        else match.group(0),
        text,
    )
    # URL/query redaction above can introduce a marker immediately after a
    # credential-shaped parameter name.  Protect it before the general
    # assignment pass so ``credential=[redacted]`` cannot be redacted a second
    # time and leave an extra closing bracket behind.
    text = text.replace("[redacted]", _REDACTION_SENTINEL)
    chunks: list[str] = []
    position = 0
    for match in _AUTHORIZATION_VALUE_RE.finditer(text):
        chunks.append(_redact_nonheader_text(text[position : match.start()]))
        chunks.append(f"{match.group('prefix')}{_redacted_complete_value(match.group('value'))}")
        position = match.end()
    chunks.append(_redact_nonheader_text(text[position:]))
    return "".join(chunks).replace(_REDACTION_SENTINEL, "[redacted]")


def sanitize_sensitive_value(
    value: Any,
    *,
    drop_sensitive_fields: bool = False,
    _active: set[int] | None = None,
) -> Any:
    """Recursively sanitize credential text and replace cyclic containers safely."""
    active = set() if _active is None else _active
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            return _CYCLE_MARKER
        active.add(identity)
        try:
            safe: dict[str, Any] = {}
            for key, item in value.items():
                safe_key = redact_sensitive_text(str(key))
                if _sensitive_fetch_field(key):
                    if not drop_sensitive_fields:
                        safe[safe_key] = "[redacted]"
                    continue
                safe[safe_key] = sanitize_sensitive_value(
                    item,
                    drop_sensitive_fields=drop_sensitive_fields,
                    _active=active,
                )
            return safe
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            return _CYCLE_MARKER
        active.add(identity)
        try:
            return [
                sanitize_sensitive_value(item, drop_sensitive_fields=drop_sensitive_fields, _active=active)
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    provider: str = ""
    rank: int = 0
    published_at: str | None = None
    author: str | None = None
    provider_score: float | None = None
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Do not use ``asdict`` here: callers can supply recursively nested
        # metadata, which would recurse before the cycle-safe sanitizer runs.
        data = sanitize_sensitive_value(
            {
                "title": self.title,
                "url": self.url,
                "snippet": self.snippet,
                "provider": self.provider,
                "rank": self.rank,
                "published_at": self.published_at,
                "author": self.author,
                "provider_score": self.provider_score,
                "content": self.content,
                "metadata": self.metadata,
            }
        )
        if not data["metadata"]:
            data.pop("metadata")
        return sanitize_sensitive_value(data)


@dataclass(slots=True)
class ProviderCall:
    provider: str
    query: str
    results: list[SearchResult] = field(default_factory=list)
    elapsed_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    http_status: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self, include_results: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "provider": self.provider,
            "query": self.query,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "result_count": len(self.results),
        }
        if self.error:
            data["error"] = self.error
        if self.http_status is not None:
            data["http_status"] = self.http_status
        if self.usage:
            data["usage"] = self.usage
        if self.metadata:
            data["metadata"] = self.metadata
        if include_results:
            data["results"] = [r.to_dict() for r in self.results]
        return sanitize_sensitive_value(data)


@dataclass(slots=True)
class FetchAttempt:
    provider: str
    status: str
    elapsed_ms: float = 0.0
    http_status: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.error, str):
            self.error = redact_sensitive_text(self.error)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "provider": self.provider,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }
        if self.http_status is not None:
            data["http_status"] = self.http_status
        if self.error:
            data["error"] = redact_sensitive_text(self.error)
        return sanitize_sensitive_value(data, drop_sensitive_fields=True)


@dataclass(slots=True)
class FetchOutcome:
    url: str
    attempts: list[FetchAttempt] = field(default_factory=list)
    selected_provider: str | None = None
    content: dict[str, Any] = field(default_factory=lambda: {"format": "unknown", "text": None})
    payload: Any | None = None

    def __post_init__(self) -> None:
        if isinstance(self.url, str):
            self.url = redact_sensitive_text(self.url)
        self.content = sanitize_sensitive_value(self.content, drop_sensitive_fields=True)
        if self.payload is not None:
            self.payload = sanitize_sensitive_value(self.payload, drop_sensitive_fields=True)

    @property
    def ok(self) -> bool:
        return self.selected_provider is not None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "url": self.url,
            "ok": self.ok,
            "selected_provider": self.selected_provider,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "content": sanitize_sensitive_value(self.content, drop_sensitive_fields=True),
        }
        if self.payload is not None:
            data["payload"] = sanitize_sensitive_value(self.payload, drop_sensitive_fields=True)
        return sanitize_sensitive_value(data, drop_sensitive_fields=True)


@dataclass(slots=True)
class SearchPlan:
    mode: str
    primary: list[str]
    fallback: list[str] = field(default_factory=list)
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    min_results: int = 5
    reason: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_sensitive_value(
            {
                "mode": self.mode,
                "primary": self.primary,
                "fallback": self.fallback,
                "provider_options": self.provider_options,
                "min_results": self.min_results,
                "reason": self.reason,
            }
        )


@dataclass(slots=True)
class SearchResponse:
    query: str
    mode: str
    plan: SearchPlan
    results: list[SearchResult]
    calls: list[ProviderCall]
    elapsed_ms: float
    warnings: list[str] = field(default_factory=list)
    execution: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "query": self.query,
            "mode": self.mode,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "plan": self.plan.to_dict(),
            "providers": [c.to_dict(include_results=False) for c in self.calls],
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
        }
        if self.execution is not None:
            data["execution"] = self.execution
        return sanitize_sensitive_value(data)
