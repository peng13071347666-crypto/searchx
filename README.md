# SearchX

SearchX is a JSON-oriented CLI for routing web searches and content extraction across configured providers. It can also show routes, make bounded research plans, collect descriptive evidence, benchmark providers, and generate route profiles.

## Install

Python 3.11 or later is required. From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
# Setup: pip may contact its package index for missing build tooling.
python -m pip install -e .
```

On Windows PowerShell, use the launcher and the virtual-environment Scripts directory instead:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

The installed command is `searchx`. Confirm the installation with `searchx --version`.

## Credentials and local checks

Search providers need their own credentials. Configure them without placing a credential value in a shell command:

```bash
# Offline/local write: prompts do not echo input.
searchx configure

# Offline: shows only configured/not-configured booleans and local-file metadata.
searchx doctor
```

`configure` stores known provider credentials in `~/.config/searchx/secrets.env` and attempts to make that file owner-readable/writable only. A process environment variable takes precedence over the local file. Supported variable names are `SERPER_API_KEY`, `BRAVE_API_KEY`, `TAVILY_API_KEY`, `EXA_API_KEY`, `NEWS_API_KEY`, `GITHUB_TOKEN`, `FIRECRAWL_API_KEY`, and `BAIDU_API_KEY`.

Keep credential values out of Git, command history, issues, and shared logs. Do not add the local secrets file or benchmark output containing sensitive queries to a repository.

## Offline commands

These commands do not call a search or extraction provider:

```bash
searchx doctor
searchx explain-route "Python async patterns" --mode auto
searchx research-plan "Compare Python async patterns" --domain docs.python.org --intensity adaptive
searchx tune /path/to/valid-benchmark-report.json --output profiles/local.json
```

`explain-route` shows the selected route. `research-plan` returns a bounded plan but does not execute it. `tune` reads a prior benchmark report and writes a profile locally.

SearchX separates route `mode` from execution `intensity`. `mode` selects the
source vertical; `intensity` controls how much of that route is executed:

| Intensity | Policy |
| --- | --- |
| `quick` | Try providers one at a time and stop at the first usable result. |
| `adaptive` | Start with one provider, then add complementary providers only for an evidence gap. This is the default. |
| `deep` | Run primary and fallback stages within a hard budget. |

Use `--max-provider-calls N` and `--max-stages N` to set hard limits. Search
responses include an `execution` object with stage metrics, evidence gaps, and a
machine-readable `stop_reason`. The model or caller may choose intensity, but
the engine enforces these limits.

## Command overview

| Command | Purpose and key options | Network / cost |
| --- | --- | --- |
| `configure` | Interactively save local credentials. | Offline; writes a local file. |
| `doctor` | Show non-sensitive configuration status. | Offline. |
| `explain-route QUERY` | Inspect routing. Supports `--mode`, `--freshness {day,week,month,year}`, repeatable `--domain`, and `--profile`. | Offline. |
| `research-plan QUERY` | Produce an offline, bounded research workflow. Supports route options plus `--intensity`, `--max-provider-calls`, and `--max-stages`. | Offline. |
| `provider PROVIDER QUERY` | Search exactly one provider. Supports `--limit`, `--mode`, `--freshness`, repeatable `--domain`, `--profile`, `--category`, `--depth`, and `--full-content`. | **Live / potentially billable.** |
| `search QUERY` | Route and fuse a search. Supports route options, `--intensity`, `--max-provider-calls`, `--max-stages`, and `--all-fallbacks`. | **Live / potentially billable.** |
| `multi-search QUERY [QUERY ...]` | Run routed searches in input order. It supports the search options; call/stage limits apply per query. | **Live / potentially billable.** |
| `fetch URL` | Extract one page through `--provider {auto,firecrawl,tavily,exa}`; supports `--profile`. | **Live / potentially billable.** |
| `evidence QUERY` | Search, then fetch selected results or repeatable explicit `--url` values. Supports the search options, `--fetch-limit`, and `--all-fallbacks`. | **Live / potentially billable.** |
| `bench` | Benchmark configured providers. Supports `--cases`, repeatable `--scenario`, repeatable `--provider`, `--max-cases`, `--workers`, `--output`/`-o`, `--full`, and `--profile`. | **Live / potentially billable.** |
| `tune REPORT` | Generate a profile from a valid benchmark report; `--input REPORT` is an alternative to the positional report, and `--output`/`-o` writes it. | Offline. |

Route modes are `auto`, `quick`, `web`, `fresh`, `news`, `code`, `academic`, `cn`, `official`, and `deep`. `bench --scenario` accepts the same scenario names; benchmark providers are `serper`, `brave`, `tavily`, `exa`, `newsapi`, `github`, `firecrawl`, and `baidu`.

## Keep live work bounded

`search` uses progressive execution by default: one primary provider, then
additional primary/fallback stages only when fused usable results or distinct
domain coverage is insufficient. `--all-fallbacks` forces all route stages
within the resolved budget. `multi-search` repeats that bounded process per
query. Start with a small result limit and explicit call/stage limits.

```bash
# LIVE — sends provider requests and may consume quota or incur cost.
searchx search "Python async patterns" --limit 3 --intensity adaptive --max-provider-calls 4 --max-stages 3

# LIVE — search plus one selected URL; automatic extraction may try more than
# one configured extraction provider until it finds usable content.
searchx evidence "Python async patterns" --fetch-limit 1

# LIVE — begin benchmarking with one provider, one case, and one worker.
searchx bench --scenario quick --provider serper --max-cases 1 --workers 1 --output reports/quick-serper.json
```

`evidence --fetch-limit` defaults to `3` and limits URLs, not individual extraction-provider attempts. `bench` schedules only configured providers, but a broad case/provider selection can still create many live calls.

## Profiles and benchmarks

Use a benchmark report as an observed, time-bound snapshot rather than a universal provider ranking. Its quality measurements depend on the selected cases and their expected terms/domains; availability, result content, freshness metadata, latency, and provider behavior can change between runs.

Typical workflow:

1. Run a deliberately small **live** `bench` command with `--output`.
2. Run offline `tune REPORT --output PROFILE`.
3. Inspect the resulting profile with offline `explain-route --profile PROFILE`, then pass `--profile PROFILE` to route-aware live commands (or set `SEARCHX_PROFILE`).

Benchmark reports can include queries, results, URLs, and provider metadata. Review them before sharing or committing them.

## Evidence is not verification

`evidence` and `research-plan` use `verification_status: "not_verified"`. Routed search results also carry descriptive evidence signals such as discovery-provider counts, agreement ratios, source domains, and timestamp parsing. These signals help prioritize review; they do not establish truth, provenance, completeness, or independent verification. Read the cited material and cross-check important claims yourself.

## Security notes

SearchX applies redaction and sanitization to CLI output and JSON files it writes, including known credential-shaped fields. Treat this as defense in depth, not an absolute guarantee: do not put secrets in queries or URLs, and inspect logs and artifacts before sharing them.

The provider HTTP client rejects redirects instead of following them with request headers, reducing the chance of forwarding credentials to a redirect destination. This does not remove the need to trust configured providers or to protect data sent in queries and fetch URLs.

## Tests

After the editable install, run:

```bash
python -m unittest discover -s tests -v
```
