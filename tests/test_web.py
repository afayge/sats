from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.cli import main
from sats.web import hot_mentions, search, social_hot


def _settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=root,
        web_search_timeout_seconds=10,
        web_search_cache_ttl_seconds=43200,
        social_hot_cache_ttl_seconds=300,
        web_search_max_results=10,
    )


def _xueqiu_html(payload: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload, ensure_ascii=False)}</script></html>'


class WebCapabilityTest(unittest.TestCase):
    def test_web_search_normalizes_results_and_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            raw = [
                {"title": "贵州茅台公告", "href": "https://example.com/a", "body": "公告摘要"},
                {"title": "字形演变 拼音 部首", "href": "https://noise.example", "body": "Unicode 释义"},
            ]
            with patch("sats.web.search._ddg_search", return_value=raw) as ddg:
                first = search("贵州茅台 最新公告", limit=5, settings=settings)
                second = search("贵州茅台 最新公告", limit=5, settings=settings)

        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["from_cache"])
        self.assertTrue(second["from_cache"])
        self.assertEqual(ddg.call_count, 1)
        self.assertEqual(len(first["results"]), 1)
        self.assertEqual(first["results"][0]["url"], "https://example.com/a")

    def test_web_search_returns_structured_error_without_caching_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            raw = [{"title": "贵州茅台公告", "href": "https://example.com/a", "body": "公告摘要"}]
            with patch("sats.web.search._ddg_search", side_effect=[TimeoutError("boom"), raw]) as ddg:
                failed = search("贵州茅台 最新公告", limit=5, settings=settings)
                recovered = search("贵州茅台 最新公告", limit=5, settings=settings)

        self.assertEqual(failed["status"], "error")
        self.assertIn("boom", failed["error"])
        self.assertFalse(failed["from_cache"])
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(ddg.call_count, 2)

    def test_social_hot_degrades_per_platform_and_mentions_match_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))

            def fake_http_json(url: str, *, settings, ua="", headers=None):
                if "weibo" in url:
                    return {"data": {"realtime": [{"word": "贵州茅台分红", "num": 900}, {"word": "AI算力", "num": 800}]}}
                return None

            with patch("sats.web.social_hot._http_json", side_effect=fake_http_json):
                hot = social_hot(platforms=["weibo", "baidu"], limit=2, settings=settings, use_cache=False)
                mentions = hot_mentions("贵州茅台", platforms=["weibo"], limit=2, settings=settings, use_cache=False)

        self.assertEqual(hot["status"], "ok")
        self.assertEqual(hot["platforms_ok"], 1)
        self.assertEqual(hot["platforms"][1]["status"], "error")
        self.assertEqual(mentions["total_hits"], 1)
        self.assertEqual(mentions["mentions"]["weibo"][0]["title"], "贵州茅台分红")

    def test_social_hot_weibo_uses_referer_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            payload = {"data": {"realtime": [{"word": "微博热搜标题", "num": 900}]}}
            with patch("sats.web.social_hot._http_json", return_value=payload) as http_json:
                hot = social_hot(platforms=["weibo"], limit=1, settings=settings, use_cache=False)

        http_json.assert_called_once()
        self.assertEqual(http_json.call_args.kwargs["headers"]["Referer"], "https://s.weibo.com/top/summary")
        self.assertEqual(hot["platforms"][0]["items"][0]["title"], "微博热搜标题")

    def test_social_hot_baidu_parses_nested_content_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            payload = {
                "success": True,
                "data": {
                    "cards": [
                        {
                            "content": [
                                {
                                    "content": [
                                        {"word": "百度热搜标题", "url": "https://m.baidu.com/s?word=a", "index": 1, "newHotName": "热"},
                                        {"word": "第二条热搜", "url": "https://m.baidu.com/s?word=b", "index": 2},
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }
            with patch("sats.web.social_hot._http_json", return_value=payload):
                hot = social_hot(platforms=["baidu"], limit=2, settings=settings, use_cache=False)

        self.assertEqual(hot["platforms"][0]["status"], "ok")
        self.assertEqual(len(hot["platforms"][0]["items"]), 2)
        self.assertEqual(hot["platforms"][0]["items"][0]["title"], "百度热搜标题")
        self.assertEqual(hot["platforms"][0]["items"][0]["extra"], "热")

    def test_social_hot_marks_empty_platform_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with patch("sats.web.social_hot._http_json", return_value={"data": {"cards": []}}):
                hot = social_hot(platforms=["baidu"], limit=2, settings=settings, use_cache=False)

        self.assertEqual(hot["status"], "error")
        self.assertEqual(hot["platforms"][0]["status"], "error")
        self.assertIn("could not be parsed", hot["platforms"][0]["error"])

    def test_social_hot_fetches_xueqiu_stock_and_spot_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            stock_html = _xueqiu_html(
                {
                    "props": {
                        "pageProps": {
                            "stockList": [
                                {"name": "贵州茅台", "symbol": "SH600519", "hotScore": 9800, "percent": "1.2%"},
                                {"name": "宁德时代", "symbol": "SZ300750", "hotScore": 7600},
                            ]
                        }
                    }
                }
            )
            spot_html = _xueqiu_html(
                {
                    "props": {
                        "pageProps": {
                            "spots": [
                                {
                                    "title": "白酒板块走强",
                                    "heat": 8800,
                                    "summary": "资金关注",
                                    "stocks": [{"name": "贵州茅台", "symbol": "SH600519"}],
                                }
                            ]
                        }
                    }
                }
            )

            def fake_http_text(url: str, *, settings, ua=""):
                return stock_html if "/hot/stock" in url else spot_html

            with (
                patch("sats.web.social_hot._http_json_with_seed", return_value=None),
                patch("sats.web.social_hot._http_text", side_effect=fake_http_text),
            ):
                both = social_hot(platforms="xueqiu", limit=2, settings=settings, use_cache=False)
                spot_only = social_hot(platforms=["雪球热点"], limit=2, settings=settings, use_cache=False)
                mentions = hot_mentions("贵州茅台", platforms="雪球", limit=2, settings=settings, use_cache=False)

        self.assertEqual([item["platform"] for item in both["platforms"]], ["xueqiu_stock", "xueqiu_spot"])
        self.assertEqual(both["platforms"][0]["items"][0]["title"], "贵州茅台 SH600519")
        self.assertEqual(both["platforms"][0]["items"][0]["url"], "https://xueqiu.com/S/SH600519")
        self.assertEqual(both["platforms"][1]["items"][0]["title"], "白酒板块走强")
        self.assertIn("贵州茅台", both["platforms"][1]["items"][0]["extra"])
        self.assertEqual([item["platform"] for item in spot_only["platforms"]], ["xueqiu_spot"])
        self.assertEqual(mentions["total_hits"], 1)
        self.assertEqual(mentions["mentions"]["xueqiu_stock"][0]["title"], "贵州茅台 SH600519")

    def test_social_hot_fetches_xueqiu_stock_from_public_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            payload = {"data": {"items": [{"name": "贵州茅台", "symbol": "SH600519", "value": 12000, "percent": "1.2%"}]}}
            with patch("sats.web.social_hot._http_json_with_seed", return_value=payload) as http_json:
                hot = social_hot(platforms="xueqiu_stock", limit=1, settings=settings, use_cache=False)

        http_json.assert_called_once()
        self.assertEqual(hot["platforms"][0]["status"], "ok")
        self.assertEqual(hot["platforms"][0]["items"][0]["title"], "贵州茅台 SH600519")
        self.assertEqual(hot["platforms"][0]["items"][0]["hot_score"], 12000)

    def test_social_hot_parses_xueqiu_visible_text_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            stock_text = "1 贵州茅台 SH600519 1.23万热度 +1.20% 加自选"
            spot_text = "1 #白酒板块走强# 贵州茅台 SH600519 +1.20 % 热度值 628.0万"

            def fake_http_text(url: str, *, settings, ua=""):
                return stock_text if "/hot/stock" in url else spot_text

            with (
                patch("sats.web.social_hot._http_json_with_seed", return_value=None),
                patch("sats.web.social_hot._http_text", side_effect=fake_http_text),
            ):
                hot = social_hot(platforms="xueqiu", limit=1, settings=settings, use_cache=False)

        self.assertEqual(hot["platforms"][0]["items"][0]["title"], "贵州茅台 SH600519")
        self.assertEqual(hot["platforms"][0]["items"][0]["hot_score"], 12300)
        self.assertEqual(hot["platforms"][1]["items"][0]["title"], "白酒板块走强")
        self.assertEqual(hot["platforms"][1]["items"][0]["hot_score"], 6280000)

    def test_social_hot_degrades_when_xueqiu_page_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))

            def fake_http_json(url: str, *, settings, ua="", headers=None):
                if "weibo" in url:
                    return {"data": {"realtime": [{"word": "AI算力", "num": 800}]}}
                return None

            with (
                patch("sats.web.social_hot._http_json", side_effect=fake_http_json),
                patch("sats.web.social_hot._http_json_with_seed", return_value=None),
                patch("sats.web.social_hot._http_text", return_value=None),
            ):
                hot = social_hot(platforms=["weibo", "xueqiu_stock"], limit=2, settings=settings, use_cache=False)

        self.assertEqual(hot["status"], "ok")
        self.assertEqual(hot["platforms_ok"], 1)
        self.assertEqual(hot["platforms"][1]["platform"], "xueqiu_stock")
        self.assertEqual(hot["platforms"][1]["status"], "error")

    def test_social_hot_uses_platform_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            payload = {"data": {"realtime": [{"word": "贵州茅台分红", "num": 900}]}}
            with patch("sats.web.social_hot._http_json", return_value=payload):
                first = social_hot(platforms=["weibo"], limit=1, settings=settings)
            with patch("sats.web.social_hot._http_json", side_effect=AssertionError("cache miss")) as http_json:
                second = social_hot(platforms=["weibo"], limit=1, settings=settings)

        self.assertEqual(first["platforms"][0]["status"], "ok")
        self.assertTrue(second["platforms"][0]["from_cache"])
        http_json.assert_not_called()

    def test_social_hot_cache_preserves_full_fetch_for_larger_later_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            payload = {"data": {"realtime": [{"word": f"热搜{i}", "num": 1000 - i} for i in range(5)]}}
            with patch("sats.web.social_hot._http_json", return_value=payload) as http_json:
                first = social_hot(platforms=["weibo"], limit=3, settings=settings)
            with patch("sats.web.social_hot._http_json", side_effect=AssertionError("cache miss")) as cached_http_json:
                second = social_hot(platforms=["weibo"], limit=5, settings=settings)

        self.assertEqual(len(first["platforms"][0]["items"]), 3)
        self.assertEqual(len(second["platforms"][0]["items"]), 5)
        self.assertTrue(second["platforms"][0]["from_cache"])
        self.assertTrue(second["platforms"][0]["cached_full_result"])
        http_json.assert_called_once()
        cached_http_json.assert_not_called()

    def test_cli_web_search_json_supports_options_after_query(self) -> None:
        stdout = StringIO()
        fake = {"status": "ok", "query": "贵州茅台 最新公告", "results": [], "fetched_at": "2026-06-09T00:00:00Z"}
        with (
            patch("sats.cli.load_settings", return_value=_settings(Path("."))),
            patch("sats.cli.web_search", return_value=fake) as web_search,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["web", "search", "贵州茅台", "最新公告", "--limit", "5", "--json"]), 0)

        web_search.assert_called_once()
        self.assertEqual(web_search.call_args.args[0], "贵州茅台 最新公告")
        self.assertEqual(web_search.call_args.kwargs["limit"], 5)
        self.assertEqual(json.loads(stdout.getvalue())["query"], "贵州茅台 最新公告")

    def test_cli_web_hot_json_passes_xueqiu_platform_alias(self) -> None:
        stdout = StringIO()
        fake = {"status": "ok", "platforms": [], "platforms_ok": 0, "platforms_checked": 2, "fetched_at": "2026-06-09T00:00:00Z"}
        with (
            patch("sats.cli.load_settings", return_value=_settings(Path("."))),
            patch("sats.cli.web_social_hot", return_value=fake) as web_hot,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["web", "hot", "--platforms", "xueqiu", "--limit", "5", "--json"]), 0)

        web_hot.assert_called_once()
        self.assertEqual(web_hot.call_args.kwargs["platforms"], ["xueqiu"])
        self.assertEqual(web_hot.call_args.kwargs["limit"], 5)
        self.assertEqual(json.loads(stdout.getvalue())["platforms_checked"], 2)
