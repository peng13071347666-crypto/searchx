from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings, finite_float, resolve_profile_path
from .models import SearchPlan


MODE_ALIASES = {
    "general": "web",
    "normal": "web",
    "latest": "fresh",
    "current": "fresh",
    "research": "deep",
    "paper": "academic",
    "papers": "academic",
    "github": "code",
    "china": "cn",
    "chinese": "cn",
}

FRESH_WORDS = {
    "latest", "current", "today", "recent", "this week", "this month", "2026",
    "最新", "当前", "今天", "今日", "最近", "本周", "本月", "刚刚", "实时",
}
NEWS_WORDS = {
    "news", "headline", "breaking", "announcement", "announced", "报道", "新闻", "头条", "发布", "宣布",
}
ACADEMIC_WORDS = {
    "paper", "papers", "arxiv", "doi", "study", "research paper", "论文", "文献", "研究论文", "学术",
}
CODE_WORDS = {
    "github", "repo", "repository", "source code", "implementation", "bug", "issue", "pull request",
    "代码", "源码", "仓库", "报错", "bug", "issue", "实现",
}
DEEP_WORDS = {
    "compare", "comparison", "evaluate", "tradeoff", "trade-off", "architecture", "landscape", "deep research",
    "对比", "比较", "评测", "选型", "优缺点", "架构", "趋势", "深度", "调研", "研究", "为什么",
}
OFFICIAL_WORDS = {"official", "documentation", "docs", "spec", "standard", "官网", "官方", "文档", "标准", "规范"}


def _contains(query: str, words: set[str]) -> bool:
    text = query.lower()
    return any(word.lower() in text for word in words)


def _contains_cjk(query: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", query))


DEFAULT_ROUTES: dict[str, dict[str, Any]] = {
    "quick": {"primary": ["serper"], "fallback": ["brave", "tavily"], "min_results": 5},
    "web": {"primary": ["serper", "brave"], "fallback": ["tavily", "exa"], "min_results": 8},
    "fresh": {"primary": ["brave", "serper"], "fallback": ["tavily", "newsapi"], "min_results": 8},
    "news": {"primary": ["newsapi", "brave", "serper"], "fallback": ["tavily", "firecrawl"], "min_results": 8},
    "code": {"primary": ["github", "serper", "exa"], "fallback": ["brave", "tavily"], "min_results": 8},
    "academic": {"primary": ["exa", "serper"], "fallback": ["tavily", "brave", "firecrawl"], "min_results": 8},
    "cn": {"primary": ["baidu", "serper"], "fallback": ["brave", "tavily", "exa"], "min_results": 8},
    "official": {"primary": ["serper", "exa"], "fallback": ["brave", "tavily"], "min_results": 8},
    "deep": {"primary": ["exa", "tavily", "serper"], "fallback": ["brave", "firecrawl"], "min_results": 12},
}


MODE_PROVIDER_OPTIONS: dict[str, dict[str, dict[str, Any]]] = {
    "fresh": {
        "brave": {"freshness": "week"},
        "serper": {"freshness": "week"},
        "tavily": {"freshness": "week"},
    },
    "news": {
        "newsapi": {"freshness": "week"},
        "brave": {"mode": "news", "freshness": "week"},
        "serper": {"mode": "news", "freshness": "week"},
        "tavily": {"mode": "news", "freshness": "week"},
        "firecrawl": {"mode": "news"},
    },
    "code": {
        "github": {"category": "repositories"},
        "exa": {"depth": "fast"},
    },
    "academic": {
        "exa": {"mode": "academic", "depth": "auto"},
        "serper": {"mode": "academic"},
        "tavily": {"depth": "advanced"},
        "firecrawl": {"mode": "academic"},
    },
    "cn": {
        "exa": {"depth": "fast"},
    },
    "official": {
        "exa": {"depth": "auto"},
    },
    "deep": {
        "exa": {"depth": "deep-lite"},
        "tavily": {"depth": "advanced"},
        "firecrawl": {"full_content": False},
    },
}


class Router:
    def __init__(self, settings: Settings, profile_path: str | None = None) -> None:
        self.settings = settings
        self.routes = json.loads(json.dumps(DEFAULT_ROUTES))
        self.mode_weights: dict[str, dict[str, float]] = {}
        path = resolve_profile_path(profile_path)
        if path:
            try:
                self._load_profile(Path(path).expanduser())
            except (TypeError, ValueError):
                pass

    def _load_profile(self, path: Path) -> None:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(obj, dict):
            return

        raw_routes = obj.get("routes")
        if isinstance(raw_routes, dict):
            for mode, route in raw_routes.items():
                if not isinstance(mode, str) or mode not in self.routes or not isinstance(route, dict):
                    continue
                for key in ("primary", "fallback"):
                    value = route.get(key)
                    if isinstance(value, list) and all(isinstance(provider, str) for provider in value):
                        self.routes[mode][key] = list(value)
                min_results = route.get("min_results")
                if isinstance(min_results, int) and not isinstance(min_results, bool) and min_results >= 0:
                    self.routes[mode]["min_results"] = min_results

        raw_weights = obj.get("mode_provider_weights")
        if not isinstance(raw_weights, dict):
            return
        for mode, weights in raw_weights.items():
            if not isinstance(mode, str) or not isinstance(weights, dict):
                continue
            parsed: dict[str, float] = {}
            for provider, value in weights.items():
                if not isinstance(provider, str):
                    continue
                number = finite_float(value)
                if number is not None:
                    parsed[provider] = number
            if parsed:
                self.mode_weights[mode] = parsed

    def infer_mode(self, query: str, requested: str = "auto") -> tuple[str, list[str]]:
        requested = MODE_ALIASES.get(requested.lower(), requested.lower())
        if requested != "auto":
            if requested not in DEFAULT_ROUTES:
                raise ValueError(f"unknown mode: {requested}")
            return requested, [f"explicit mode={requested}"]

        reasons: list[str] = []
        if _contains(query, CODE_WORDS):
            reasons.append("code/repository intent")
            return "code", reasons
        if _contains(query, ACADEMIC_WORDS):
            reasons.append("academic intent")
            return "academic", reasons
        if _contains(query, NEWS_WORDS) and _contains(query, FRESH_WORDS):
            reasons.append("news + freshness intent")
            return "news", reasons
        if _contains(query, DEEP_WORDS):
            reasons.append("comparison/research complexity")
            return "deep", reasons
        if _contains(query, OFFICIAL_WORDS):
            reasons.append("official/documentation intent")
            return "official", reasons
        if _contains(query, FRESH_WORDS):
            reasons.append("freshness intent")
            return "fresh", reasons
        if _contains_cjk(query):
            reasons.append("CJK/Chinese query")
            return "cn", reasons
        reasons.append("general web discovery")
        return "web", reasons

    def plan(
        self,
        query: str,
        requested_mode: str = "auto",
        *,
        freshness: str | None = None,
        domains: list[str] | None = None,
    ) -> SearchPlan:
        mode, reason = self.infer_mode(query, requested_mode)
        route = self.routes[mode]
        options = json.loads(json.dumps(MODE_PROVIDER_OPTIONS.get(mode, {})))
        all_providers = list(dict.fromkeys(route["primary"] + route["fallback"]))
        for provider in all_providers:
            provider_opt = options.setdefault(provider, {})
            provider_opt.setdefault("mode", mode)
            if freshness:
                provider_opt["freshness"] = freshness
            if domains:
                provider_opt["domains"] = domains

        # Refine GitHub vertical based on wording instead of spawning a separate CLI.
        if mode == "code":
            lowered = query.lower()
            kind = "repositories"
            if any(word in lowered for word in (" issue", "bug", "报错", "问题单")):
                kind = "issues"
            elif any(word in lowered for word in ("source code", "代码搜索", "code search", "function ", "class ")):
                kind = "code"
            options.setdefault("github", {})["category"] = kind

        return SearchPlan(
            mode=mode,
            primary=list(route["primary"]),
            fallback=list(route["fallback"]),
            provider_options=options,
            min_results=int(route.get("min_results", 5)),
            reason=reason,
        )

    def weights_for(self, mode: str) -> dict[str, float]:
        weights = dict(self.settings.provider_weights)
        weights.update(self.mode_weights.get(mode, {}))
        return weights
