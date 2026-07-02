from __future__ import annotations

import json
import tempfile
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.agent.tools import build_default_tool_registry
from sats.agent.tools.base import AgentToolContext
from sats.agent.models import AgentExecutionPolicy
from sats.skillhub import (
    SkillHubSkill,
    fetch_skillhub_catalog,
    load_skillhub_manifest,
    search_local_skillhub_records,
    skillhub_status,
    sync_skillhub_skills,
)
from sats.skills import load_skills, match_skills


def _record(
    uuid: str,
    name: str,
    cn_name: str,
    description: str,
    *,
    classify: str = "OFFICIAL",
    storage_path: str = "",
) -> SkillHubSkill:
    return SkillHubSkill(
        skill_uuid=uuid,
        name=name,
        cn_name=cn_name,
        description=description,
        classify=classify,
        version="1.0.0",
        storage_path=storage_path,
        author="tester",
    )


class SkillHubTest(unittest.TestCase):
    def test_sync_generates_local_skills_manifest_and_matchable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = [
                _record(
                    "aaaaaaaa-1111-2222-3333-444444444444",
                    "report-search",
                    "研报搜索",
                    "收录主流投研机构发布的研究报告，支持投资评级和目标价查询。",
                    storage_path="s3:iwencai/uuid/1.0.0/report-search.zip",
                ),
                _record(
                    "bbbbbbbb-1111-2222-3333-444444444444",
                    "波动率策略",
                    "波动率策略",
                    "期权波动率和策略分析。",
                    classify="THIRD_PARTY",
                    storage_path="s3:iwencai/uuid/1.0.0/volatility.zip",
                ),
            ]

            result = sync_skillhub_skills(root, records=records)

            self.assertEqual(result.total, 2)
            self.assertEqual(result.installed, 2)
            manifest = load_skillhub_manifest(root / "skills")
            self.assertEqual(manifest["count"], 2)
            skills = load_skills(root / "skills")
            skill_ids = {item.id for item in skills}
            self.assertIn("skillhub-report-search", skill_ids)
            self.assertIn("skillhub-volatility", skill_ids)
            matched = [item.id for item in match_skills("帮我做研报搜索", skills)]
            self.assertIn("skillhub-report-search", matched)

    def test_sync_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = sync_skillhub_skills(
                root,
                records=[_record("aaaaaaaa", "news-search", "新闻搜索", "财经新闻搜索。")],
                dry_run=True,
            )

            self.assertEqual(result.installed, 1)
            self.assertFalse((root / "skills").exists())

    def test_sync_prunes_only_generated_skillhub_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = [
                _record("aaaaaaaa", "news-search", "新闻搜索", "财经新闻搜索。"),
                _record("bbbbbbbb", "report-search", "研报搜索", "研报搜索。"),
            ]
            sync_skillhub_skills(root, records=first)
            manual = root / "skills" / "skillhub-manual" / "SKILL.md"
            manual.parent.mkdir(parents=True)
            manual.write_text("---\nname: manual\n---\nmanual\n", encoding="utf-8")

            result = sync_skillhub_skills(root, records=first[:1], prune_generated=True)

            self.assertEqual(result.removed, 1)
            self.assertTrue((root / "skills" / "skillhub-news-search" / "SKILL.md").exists())
            self.assertFalse((root / "skills" / "skillhub-report-search").exists())
            self.assertTrue(manual.exists())

    def test_search_and_status_use_local_manifest_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_skillhub_skills(root, records=[_record("aaaaaaaa", "macro-query", "宏观数据查询", "查询 GDP、CPI、社融等宏观经济指标。")])

            rows = search_local_skillhub_records(root, query="CPI")
            status = skillhub_status(root, api_key="secret-key", base_url="https://openapi.iwencai.com", cli_name="missing-cli")

            self.assertEqual(rows[0]["id"], "skillhub-macro-query")
            self.assertTrue(status["iwencai_api_key_configured"])
            self.assertNotIn("secret-key", json.dumps(status, ensure_ascii=False))

    def test_default_agent_registry_exposes_skillhub_tools(self) -> None:
        registry = build_default_tool_registry()

        self.assertIn("skillhub.search", registry.names())
        self.assertIn("skillhub.load", registry.names())
        self.assertIn("skillhub.status", registry.names())

    def test_skillhub_search_tool_reads_generated_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_skillhub_skills(root, records=[_record("aaaaaaaa", "announcement-search", "公告搜索", "支持 A 股公告搜索。")])
            registry = build_default_tool_registry()
            context = AgentToolContext(
                settings=SimpleNamespace(project_root=root),
                storage=None,
                resolver=None,
                policy=AgentExecutionPolicy(),
                command_runner=None,
                trader=None,
            )

            result = registry.execute("skillhub.search", {"query": "公告"}, context)

            self.assertEqual(result.status, "done")
            self.assertIn("公告搜索", result.content)

    def test_fetch_skillhub_catalog_paginates_remote_response(self) -> None:
        pages = {
            "1": {
                "status_code": 0,
                "data": {
                    "total": 2,
                    "total_pages": 2,
                    "records": [
                        {
                            "skill_uuid": "aaaaaaaa",
                            "name": "news-search",
                            "cn_name": "新闻搜索",
                            "description": "当用户询问新闻搜索时，必须使用此技能。",
                            "classify": "OFFICIAL",
                            "version": "1.0.0",
                            "storage_path": "s3:iwencai/a/1.0.0/news-search.zip",
                        }
                    ],
                },
            },
            "2": {
                "status_code": 0,
                "data": {
                    "total": 2,
                    "total_pages": 2,
                    "records": [
                        {
                            "skill_uuid": "bbbbbbbb",
                            "name": "report-search",
                            "cn_name": "研报搜索",
                            "description": "研报搜索",
                            "classify": "OFFICIAL",
                            "version": "1.0.0",
                            "storage_path": "s3:iwencai/b/1.0.0/report-search.zip",
                        }
                    ],
                },
            },
        }

        class Response:
            def __init__(self, payload: dict) -> None:
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(request.full_url).query))
            return Response(pages[query["current"]])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            records = fetch_skillhub_catalog(page_size=1)

        self.assertEqual([item.name for item in records], ["news-search", "report-search"])
        self.assertNotIn("必须使用此技能", records[0].description)
        self.assertIn("适用于", records[0].description)


if __name__ == "__main__":
    unittest.main()
