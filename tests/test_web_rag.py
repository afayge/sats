from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from sats.cli import main
from sats.web import open_page, search
from sats.web.fetch import fetch_page, validate_public_url
from sats.web.index import WebIndex
from sats.web.providers import _bing_search, _bocha_search, _querit_search, _tavily_search
from sats.web.rag_search import _embed_texts, validate_citations


def _settings(root: Path, **overrides) -> SimpleNamespace:
    values = {
        "project_root": root,
        "db_path": root / "sats.duckdb",
        "web_search_timeout_seconds": 5,
        "web_search_cache_ttl_seconds": 3600,
        "web_page_cache_ttl_seconds": 86400,
        "web_search_max_results": 10,
        "web_search_backend": "auto",
        "web_search_providers": "ddgs",
        "web_search_context_size": "auto",
        "web_responses_base_url": "https://api.example/v1",
        "web_responses_api_key": "responses-key",
        "web_responses_model": "responses-model",
        "web_embedding_provider": "none",
        "web_embedding_base_url": "",
        "web_embedding_api_key": "",
        "web_embedding_model": "",
        "web_fastembed_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "light_model_name": "",
        "openai_model": "",
        "llm_timeout_seconds": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WebRagTest(unittest.TestCase):
    def test_auto_prefers_native_rag_even_when_responses_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            raw = [{"title": "公告", "href": "https://example.com/a", "body": "公司发布最新公告"}]
            page = {
                "url": "https://example.com/a",
                "title": "公告",
                "content": "公司发布最新公告，内容可追溯。",
                "content_type": "text/plain",
                "extraction_method": "plain_text",
                "published_at": None,
            }
            with (
                patch("sats.web.search._ddg_search", return_value=raw),
                patch("sats.web.rag_search.fetch_page", return_value=page),
                patch("openai.OpenAI", side_effect=AssertionError("Responses must not be called")),
            ):
                payload = search("公司最新公告", settings=settings, use_cache=False)

        self.assertEqual(payload["backend"], "rag")
        self.assertEqual(payload["queries"], ["公司最新公告"])
        self.assertEqual(payload["providers"][0]["provider"], "ddgs")
        self.assertEqual(payload["sources"][0]["id"], "S1")
        self.assertIn("[S1]", payload["answer"])
        self.assertTrue(payload["evidence"])

    def test_rag_enforces_domain_filter_before_page_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            raw = [
                {"title": "允许", "href": "https://news.sse.com.cn/a", "body": "公告摘要"},
                {"title": "伪装", "href": "https://sse.com.cn.evil.example/b", "body": "错误摘要"},
            ]
            page = {
                "url": "https://news.sse.com.cn/a",
                "title": "允许",
                "content": "上交所公告正文。",
                "content_type": "text/plain",
                "extraction_method": "plain_text",
                "published_at": None,
            }
            with (
                patch("sats.web.search._ddg_search", return_value=raw),
                patch("sats.web.rag_search.fetch_page", return_value=page) as fetch,
            ):
                payload = search(
                    "公告",
                    trusted_domains=["sse.com.cn"],
                    settings=settings,
                    use_cache=False,
                )

        fetch.assert_called_once()
        self.assertEqual(payload["sources"][0]["url"], "https://news.sse.com.cn/a")

    def test_validate_public_url_rejects_loopback_resolution(self) -> None:
        with patch("sats.web.fetch._resolve_addresses", return_value=("127.0.0.1",)):
            with self.assertRaisesRegex(ValueError, "non-public"):
                validate_public_url("https://example.com/private")

    def test_fetch_page_revalidates_redirect_target(self) -> None:
        with (
            patch("sats.web.fetch.validate_public_url") as validate,
            patch(
                "sats.web.fetch._http_get_once",
                side_effect=[
                    (302, {"location": "https://cdn.example.com/page"}, b""),
                    (200, {"content-type": "text/plain"}, b"redirected content"),
                ],
            ),
        ):
            payload = fetch_page("https://example.com/start", settings=_settings(Path(".")))

        self.assertEqual(
            validate.call_args_list,
            [
                call("https://example.com/start", trusted_domains=()),
                call("https://cdn.example.com/page", trusted_domains=()),
            ],
        )
        self.assertEqual(payload["url"], "https://cdn.example.com/page")
        self.assertEqual(payload["content"], "redirected content")

    def test_web_index_separates_embedding_models_and_clears_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), web_page_cache_ttl_seconds=60)
            index = WebIndex(settings)
            document = index.put_document(
                url="https://example.com/a",
                title="A",
                content="alpha beta gamma " * 200,
                content_type="text/plain",
                extraction_method="test",
            )
            chunks = index.chunks_for_documents([document["document_id"]])
            chunk_id = chunks[0]["chunk_id"]
            index.put_embeddings({chunk_id: [1.0, 0.0]}, provider="openai", model="m1")
            index.put_embeddings({chunk_id: [0.0, 1.0]}, provider="openai", model="m2")

            self.assertEqual(index.get_embeddings([chunk_id], provider="openai", model="m1")[chunk_id], [1.0, 0.0])
            self.assertEqual(index.get_embeddings([chunk_id], provider="openai", model="m2")[chunk_id], [0.0, 1.0])
            cleared = index.clear()

        self.assertEqual(cleared["documents"], 1)
        self.assertGreater(cleared["chunks"], 0)
        self.assertEqual(cleared["embeddings"], 2)

    def test_openai_compatible_embeddings_use_batch_input_and_float_encoding(self) -> None:
        create = Mock(
            return_value=SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[1, 0]),
                    SimpleNamespace(embedding=[0, 1]),
                ]
            )
        )
        client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        settings = _settings(
            Path("."),
            web_embedding_base_url="https://embeddings.example/v1",
            web_embedding_api_key="key",
        )
        with patch("openai.OpenAI", return_value=client):
            vectors = _embed_texts(["a", "b"], provider="openai", model="embed-model", settings=settings)

        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(create.call_args.kwargs["input"], ["a", "b"])
        self.assertEqual(create.call_args.kwargs["encoding_format"], "float")

    def test_provider_adapters_parse_bing_tavily_bocha_and_querit(self) -> None:
        settings = _settings(
            Path("."),
            web_tavily_api_key="tavily",
            web_bocha_api_key="bocha",
            web_querit_api_key="querit",
        )
        bing_response = Mock(
            text=(
                '<li class="b_algo"><h2><a href="https://example.com/bing">Bing Title</a></h2>'
                "<div class=\"b_caption\"><p>Bing snippet</p></div></li>"
            )
        )
        bing_response.raise_for_status.return_value = None
        with patch("sats.web.providers.httpx.get", return_value=bing_response):
            bing = _bing_search("query", settings=settings, max_results=5, domains=())

        tavily_response = Mock()
        tavily_response.raise_for_status.return_value = None
        tavily_response.json.return_value = {
            "results": [{"title": "Tavily", "url": "https://example.com/t", "content": "Tavily snippet"}]
        }
        bocha_response = Mock()
        bocha_response.raise_for_status.return_value = None
        bocha_response.json.return_value = {
            "code": 200,
            "data": {"webPages": {"value": [{"name": "BoCha", "url": "https://example.com/b", "summary": "BoCha snippet"}]}},
        }
        querit_response = Mock()
        querit_response.raise_for_status.return_value = None
        querit_response.json.return_value = {
            "error_code": 200,
            "results": {"result": [{"title": "Querit", "url": "https://example.com/q", "snippet": "Querit snippet"}]},
        }
        with patch(
            "sats.web.providers.httpx.post",
            side_effect=[tavily_response, bocha_response, querit_response],
        ):
            tavily = _tavily_search("query", settings=settings, max_results=5, domains=())
            bocha = _bocha_search("query", settings=settings, max_results=5, domains=())
            querit = _querit_search("query", settings=settings, max_results=5, freshness="")

        self.assertEqual(bing[0]["title"], "Bing Title")
        self.assertEqual(tavily[0]["source"], "tavily")
        self.assertEqual(bocha[0]["source"], "bocha")
        self.assertEqual(querit[0]["source"], "querit")

    def test_invalid_citation_ids_are_removed(self) -> None:
        self.assertEqual(validate_citations("事实 [S1] 错误 [S9]", {"S1"}), "事实 [S1] 错误")

    def test_open_page_uses_cached_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            index = WebIndex(settings)
            index.put_document(
                url="https://example.com/a",
                title="Cached",
                content="cached page body",
                content_type="text/plain",
                extraction_method="test",
            )
            with patch("sats.web.rag_search.fetch_page", side_effect=AssertionError("cache miss")):
                payload = open_page("https://example.com/a", settings=settings)

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["from_cache"])
        self.assertEqual(payload["sources"][0]["id"], "S1")

    def test_cli_web_open_and_cache_clear_dispatch(self) -> None:
        stdout = StringIO()
        settings = _settings(Path("."))
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch(
                "sats.cli.web_open_page",
                return_value={"status": "ok", "url": "https://example.com", "title": "Example", "content": "body"},
            ) as web_open,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["web", "open", "https://example.com", "--query", "body", "--json"]), 0)
        web_open.assert_called_once()
        self.assertEqual(web_open.call_args.kwargs["query"], "body")

        stdout = StringIO()
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch(
                "sats.cli.clear_web_cache",
                return_value={"status": "ok", "documents": 1, "chunks": 2, "embeddings": 3, "expired_only": True},
            ) as clear,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["web", "cache", "clear", "--expired-only", "--json"]), 0)
        clear.assert_called_once_with(settings=settings, expired_only=True)


if __name__ == "__main__":
    unittest.main()
