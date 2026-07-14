from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sats.cli import main
from sats.agent.planner import _web_steps
from sats.agent.tools import build_default_tool_registry
from sats.web import anysearch
from sats.web.providers import ProviderResult, _rrf_merge, configured_provider_names, search_many
from sats.web.rag_search import open_page
from sats.web.search import batch_search, get_sub_domains, search


def _settings(root: Path, **overrides) -> SimpleNamespace:
    values = {
        "project_root": root,
        "db_path": root / "sats.duckdb",
        "anysearch_api_key": "",
        "web_search_timeout_seconds": 5,
        "web_search_cache_ttl_seconds": 3600,
        "web_page_cache_ttl_seconds": 86400,
        "web_search_max_results": 10,
        "web_search_backend": "rag",
        "web_search_providers": "anysearch,ddgs,bing",
        "web_search_context_size": "medium",
        "web_embedding_provider": "none",
        "web_embedding_base_url": "",
        "web_embedding_api_key": "",
        "web_embedding_model": "",
        "web_fastembed_model": "",
        "light_model_name": "",
        "openai_model": "",
        "llm_timeout_seconds": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AnySearchClientTest(unittest.TestCase):
    def test_search_builds_json_rpc_and_optional_auth(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "result": {
                "_meta": {"request_id": "req"},
                "content": [
                    {
                        "type": "text",
                        "text": "## Search Results (1 results, 1ms)\n\n### 1. Result\n- **URL**: https://example.com/a\n- body",
                    }
                ],
            }
        }
        settings = _settings(Path("."), anysearch_api_key="secret-value")
        with patch("sats.web.anysearch.httpx.post", return_value=response) as post:
            result = anysearch.search("query", settings=settings, max_results=2)

        request = post.call_args.kwargs
        self.assertEqual(request["json"]["method"], "tools/call")
        self.assertEqual(request["json"]["params"]["name"], "search")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-value")
        self.assertEqual(result.items[0]["url"], "https://example.com/a")

    def test_anonymous_request_and_is_error(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "result": {"content": [{"type": "text", "text": "quota exhausted"}], "isError": True}
        }
        with patch("sats.web.anysearch.httpx.post", return_value=response) as post:
            with self.assertRaisesRegex(anysearch.AnySearchError, "quota exhausted"):
                anysearch.search("query", settings=_settings(Path(".")), max_results=2)
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_batch_and_domain_markdown_parsers(self) -> None:
        batch_text = (
            "## Query 1: A\n\n## Search Results (1 results, 1ms)\n\n"
            "### 1. A result\n- **URL**: https://example.com/a\n- alpha\n\n"
            "## Query 2: B\n\n## Search Results (1 results, 1ms)\n\n"
            "### 1. B result\n- **URL**: https://example.com/b\n- beta"
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": {"content": [{"type": "text", "text": batch_text}]}}
        with patch("sats.web.anysearch.httpx.post", return_value=response):
            results = anysearch.batch_search([{"query": "A"}, {"query": "B"}], settings=_settings(Path(".")))
        self.assertEqual([item.items[0]["title"] for item in results], ["A result", "B result"])

        domains = anysearch.parse_sub_domains(
            "### finance.quote\nQuotes\n\n**Parameters:**\n- `type` (required): asset type\n- `period`: range"
        )
        self.assertEqual(domains[0]["sub_domain"], "finance.quote")
        self.assertTrue(domains[0]["params"][0]["required"])

    def test_weighted_rrf_prefers_anysearch(self) -> None:
        rows = _rrf_merge(
            [
                ProviderResult(
                    provider="ddgs",
                    status="ok",
                    items=({"url": "https://example.com/ddg", "title": "DDG", "provider": "ddgs"},),
                ),
                ProviderResult(
                    provider="anysearch",
                    status="ok",
                    items=({"url": "https://example.com/any", "title": "Any", "provider": "anysearch"},),
                ),
            ]
        )
        self.assertEqual(rows[0]["title"], "Any")
        self.assertGreater(rows[0]["search_score"], rows[1]["search_score"])

    def test_default_provider_order_includes_anysearch(self) -> None:
        self.assertEqual(configured_provider_names(_settings(Path("."))), ("anysearch", "ddgs", "bing"))

    def test_provider_layer_batches_expanded_anysearch_queries(self) -> None:
        responses = [
            anysearch.AnySearchResponse("", ({"title": "A", "url": "https://example.com/a", "snippet": "a"},)),
            anysearch.AnySearchResponse("", ({"title": "B", "url": "https://example.com/b", "snippet": "b"},)),
        ]
        with patch("sats.web.providers.anysearch.batch_search", return_value=responses) as batch:
            rows, status = search_many(
                ["A", "B"],
                provider_names=("anysearch",),
                settings=_settings(Path(".")),
                max_results=5,
                freshness="",
                domains=(),
                ddgs_searcher=Mock(),
            )
        batch.assert_called_once()
        self.assertEqual(len(rows), 2)
        self.assertEqual(status[0]["result_count"], 2)


class AnySearchIntegrationTest(unittest.TestCase):
    def test_chat_registry_and_fallback_planner_expose_new_web_tools(self) -> None:
        registry = build_default_tool_registry()
        self.assertIn("web.get_sub_domains", registry.names())
        self.assertIn("web.batch_search", registry.names())
        steps = _web_steps("请分别搜索 OpenAI 最新文档；DuckDB 最新版本")
        self.assertEqual(steps[0].tool_name, "web.batch_search")
        self.assertEqual([item["query"] for item in steps[0].arguments["queries"]], ["OpenAI 最新文档", "DuckDB 最新版本"])

    def test_vertical_search_requires_discovered_params_and_blocks_astock_quote(self) -> None:
        catalog = {
            "status": "ok",
            "items": [
                {
                    "sub_domain": "finance.quote",
                    "params": [
                        {"name": "type", "required": True},
                        {"name": "symbol", "required": True},
                        {"name": "cn_code", "required": True},
                    ],
                }
            ],
        }
        with patch("sats.web.search.get_sub_domains", return_value=catalog):
            missing = search(
                "AAPL",
                domain="finance",
                sub_domain="finance.quote",
                sub_domain_params={"type": "stock"},
                settings=_settings(Path(".")),
                use_cache=False,
            )
            blocked = search(
                "600519.SH",
                domain="finance",
                sub_domain="finance.quote",
                sub_domain_params={"type": "stock", "symbol": "", "cn_code": "600519.SH"},
                settings=_settings(Path(".")),
                use_cache=False,
            )
        self.assertIn("missing required", missing["error"])
        self.assertIn("AStockDataProvider", blocked["error"])

    def test_get_sub_domains_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            data = {"raw_markdown": "### finance.news\nNews", "items": [{"sub_domain": "finance.news"}], "meta": {}}
            with patch("sats.web.search.anysearch.get_sub_domains", return_value=data) as call:
                first = get_sub_domains(["finance"], settings=settings)
                second = get_sub_domains(["finance"], settings=settings)
        self.assertEqual(first["status"], "ok")
        self.assertTrue(second["from_cache"])
        call.assert_called_once()

    def test_batch_uses_anysearch_prefetch_and_preserves_order(self) -> None:
        settings = _settings(Path("."), web_search_providers="anysearch")
        responses = [
            anysearch.AnySearchResponse("", ({"title": "A", "url": "https://example.com/a", "snippet": "a"},)),
            anysearch.AnySearchResponse("", ({"title": "B", "url": "https://example.com/b", "snippet": "b"},)),
        ]
        fake_payloads = [
            {"status": "ok", "query": "A", "warnings": [], "sources": [], "results": []},
            {"status": "ok", "query": "B", "warnings": [], "sources": [], "results": []},
        ]
        with (
            patch("sats.web.search.anysearch.batch_search", return_value=responses) as upstream,
            patch("sats.web.search.search", side_effect=fake_payloads) as single,
        ):
            payload = batch_search(["A", "B"], settings=settings)
        upstream.assert_called_once()
        self.assertEqual(single.call_count, 2)
        self.assertEqual(payload["queries"], ["A", "B"])

    def test_open_page_prefers_extract_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with (
                patch("sats.web.rag_search.validate_public_url"),
                patch(
                    "sats.web.rag_search.anysearch.extract",
                    return_value={"title": "Remote", "content": "remote body"},
                ),
                patch("sats.web.rag_search.fetch_page", side_effect=AssertionError("native fetch should not run")),
            ):
                remote = open_page("https://example.com/remote", settings=settings, use_cache=False)
            self.assertEqual(remote["backend"], "anysearch")

            with (
                patch("sats.web.rag_search.validate_public_url"),
                patch("sats.web.rag_search.anysearch.extract", side_effect=RuntimeError("down")),
                patch(
                    "sats.web.rag_search.fetch_page",
                    return_value={
                        "url": "https://example.com/native",
                        "title": "Native",
                        "content": "native body",
                        "content_type": "text/plain",
                        "extraction_method": "plain_text",
                        "published_at": None,
                    },
                ),
            ):
                native = open_page("https://example.com/native", settings=settings, use_cache=False)
            self.assertEqual(native["backend"], "rag")
            self.assertTrue(native["degraded"])

    def test_cli_domains_and_batch_dispatch(self) -> None:
        settings = _settings(Path("."))
        stdout = StringIO()
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch(
                "sats.cli.web_get_sub_domains",
                return_value={"status": "ok", "raw_markdown": "### finance.news", "items": []},
            ) as domains,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["web", "domains", "--domain", "finance", "--json"]), 0)
        domains.assert_called_once()

        stdout = StringIO()
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch(
                "sats.cli.web_batch_search",
                return_value={"status": "ok", "queries": ["A", "B"], "results": [], "succeeded": 2, "failed": 0},
            ) as batch,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["web", "batch", "--query", "A", "--query", "B", "--json"]), 0)
        self.assertEqual([item["query"] for item in batch.call_args.args[0]], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
