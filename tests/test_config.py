from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sats.config as config_mod
from sats.cli import main
from sats.config import DEFAULT_ENV_CONTENT, load_settings


class ConfigTest(unittest.TestCase):
    def test_model_profiles_are_resolved_from_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "DEEPSEEK_PROVIDER=deepseek\n"
                "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1\n"
                "DEEPSEEK_API_KEY=ds-key\n"
                "DEEPSEEK_MODEL=deepseek-chat\n"
                "DEEPSEEK_LIGHT_MODEL=deepseek-lite\n"
                "XIAOMIMIMO_PROVIDER=mimo\n"
                "XIAOMIMIMO_BASE_URL=https://api.xiaomimimo.com/v1\n"
                "XIAOMIMIMO_API_KEY=mimo-key\n"
                "XIAOMIMIMO_MODEL=MiMo-main\n"
                "XIAOMIMIMO_LIGHT_MODEL=MiMo-light\n"
                "DEFAULT_MODEL=DEEPSEEK\n"
                "DEFAULT_LIGHT_MODEL=XIAOMIMIMO\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(project_root=root)

        self.assertEqual(settings.llm_profile, "DEEPSEEK")
        self.assertEqual(settings.llm_provider, "deepseek")
        self.assertEqual(settings.openai_model, "deepseek-chat")
        self.assertEqual(settings.openai_api_key, "ds-key")
        self.assertEqual(settings.light_llm_profile, "XIAOMIMIMO")
        self.assertEqual(settings.light_llm_provider, "mimo")
        self.assertEqual(settings.light_model_name, "MiMo-light")

    def test_light_llm_follows_main_when_default_light_model_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "QWEN_PROVIDER=qwen\n"
                "QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
                "QWEN_API_KEY=qwen-key\n"
                "QWEN_MODEL=qwen-plus\n"
                "DEFAULT_MODEL=QWEN\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(project_root=root)

        self.assertEqual(settings.llm_provider, "qwen")
        self.assertEqual(settings.openai_model, "qwen-plus")
        self.assertEqual(settings.light_llm_provider, "qwen")
        self.assertEqual(settings.light_model_name, "qwen-plus")

    def test_unknown_default_model_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "DEEPSEEK_PROVIDER=deepseek\nDEEPSEEK_MODEL=deepseek-chat\nDEFAULT_MODEL=UNKNOWN\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "模型配置组不存在: UNKNOWN"):
                    load_settings(project_root=root)

    def test_legacy_llm_variables_no_longer_configure_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("LANGCHAIN_PROVIDER=deepseek\nOPENAI_MODEL=deepseek-v4-pro\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "旧 LLM 配置已移除"):
                    load_settings(project_root=root)

    def test_default_root_falls_back_to_package_project_when_cwd_has_no_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "project"
            package_root.mkdir()
            cwd = Path(tmp) / "outside"
            cwd.mkdir()
            (package_root / ".env").write_text(
                "DEEPSEEK_PROVIDER=deepseek\nDEEPSEEK_MODEL=deepseek-chat\nDEFAULT_MODEL=DEEPSEEK\n"
                "SATS_DB_PATH=data/test.duckdb\n",
                encoding="utf-8",
            )
            fake_config_path = package_root / "sats" / "config.py"
            fake_config_path.parent.mkdir()
            fake_config_path.write_text("", encoding="utf-8")

            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(config_mod, "__file__", str(fake_config_path)),
                patch("pathlib.Path.cwd", return_value=cwd),
            ):
                settings = load_settings()

        self.assertEqual(settings.project_root, package_root.resolve())
        self.assertEqual(settings.env_path, (package_root / ".env").resolve())
        self.assertEqual(settings.openai_model, "deepseek-chat")
        self.assertEqual(settings.db_path, package_root.resolve() / "data/test.duckdb")

    def test_current_directory_env_still_takes_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".env").write_text(
                "MIMO_PROVIDER=mimo\nMIMO_MODEL=cwd-model\nDEFAULT_MODEL=MIMO\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=cwd):
                settings = load_settings()

        self.assertEqual(settings.project_root, cwd.resolve())
        self.assertEqual(settings.openai_model, "cwd-model")

    def test_explicit_env_path_controls_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom"
            root.mkdir()
            env_path = root / "custom.env"
            env_path.write_text("CUSTOM_PROVIDER=openai\nCUSTOM_MODEL=explicit-model\nDEFAULT_MODEL=CUSTOM\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(env_path=env_path)

        self.assertEqual(settings.project_root, root.resolve())
        self.assertEqual(settings.env_path, env_path.resolve())
        self.assertEqual(settings.openai_model, "explicit-model")

    def test_default_env_content_uses_new_llm_variables_only(self) -> None:
        self.assertIn("DEEPSEEK_PROVIDER=deepseek", DEFAULT_ENV_CONTENT)
        self.assertIn("DEEPSEEK_MODEL=deepseek-chat", DEFAULT_ENV_CONTENT)
        self.assertIn("XIAOMIMIMO_PROVIDER=mimo", DEFAULT_ENV_CONTENT)
        self.assertIn("XIAOMIMIMO_MODEL=MiMo-72B-A27B", DEFAULT_ENV_CONTENT)
        self.assertIn("XIAOMIMIMO_LIGHT_MODEL=MiMo-72B-A27B", DEFAULT_ENV_CONTENT)
        self.assertIn("DEFAULT_MODEL=DEEPSEEK", DEFAULT_ENV_CONTENT)
        self.assertIn("DEFAULT_LIGHT_MODEL=XIAOMIMIMO", DEFAULT_ENV_CONTENT)
        self.assertIn("LLM_TEMPERATURE=0.0", DEFAULT_ENV_CONTENT)
        self.assertIn("WEB_SEARCH_PROVIDERS=anysearch,ddgs,bing", DEFAULT_ENV_CONTENT)
        self.assertIn("ANYSEARCH_API_KEY=", DEFAULT_ENV_CONTENT)
        self.assertIn("WEB_EMBEDDING_PROVIDER=auto", DEFAULT_ENV_CONTENT)
        self.assertIn("WEB_PAGE_CACHE_TTL_SECONDS=86400", DEFAULT_ENV_CONTENT)
        self.assertNotIn("LANGCHAIN_PROVIDER=", DEFAULT_ENV_CONTENT)
        self.assertNotIn("OPENAI_API_KEY=", DEFAULT_ENV_CONTENT)
        self.assertNotIn("LLM_PROVIDER=", DEFAULT_ENV_CONTENT)
        self.assertNotIn("OPENAI_MODEL=", DEFAULT_ENV_CONTENT)

    def test_web_responses_configuration_is_loaded_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "DEEPSEEK_PROVIDER=deepseek\n"
                "DEEPSEEK_MODEL=deepseek-chat\n"
                "DEFAULT_MODEL=DEEPSEEK\n"
                "WEB_SEARCH_BACKEND=responses\n"
                "WEB_SEARCH_PROVIDERS=anysearch,ddgs,bing,tavily\n"
                "ANYSEARCH_API_KEY=anysearch-key\n"
                "WEB_PAGE_CACHE_TTL_SECONDS=7200\n"
                "WEB_RESPONSES_BASE_URL=http://127.0.0.1:8080/v1\n"
                "WEB_RESPONSES_API_KEY=test-key\n"
                "WEB_RESPONSES_MODEL=openrouter/deepseek/deepseek-r1\n"
                "WEB_SEARCH_CONTEXT_SIZE=high\n"
                "WEB_TAVILY_API_KEY=tavily-key\n"
                "WEB_EMBEDDING_PROVIDER=openai\n"
                "WEB_EMBEDDING_BASE_URL=http://127.0.0.1:8081/v1\n"
                "WEB_EMBEDDING_API_KEY=embedding-key\n"
                "WEB_EMBEDDING_MODEL=text-embedding-test\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(project_root=root)

        self.assertEqual(settings.web_search_backend, "responses")
        self.assertEqual(settings.web_responses_base_url, "http://127.0.0.1:8080/v1")
        self.assertEqual(settings.web_responses_model, "openrouter/deepseek/deepseek-r1")
        self.assertEqual(settings.web_search_context_size, "high")
        self.assertEqual(settings.web_search_providers, "anysearch,ddgs,bing,tavily")
        self.assertEqual(settings.anysearch_api_key, "anysearch-key")
        self.assertEqual(settings.web_page_cache_ttl_seconds, 7200)
        self.assertEqual(settings.web_tavily_api_key, "tavily-key")
        self.assertEqual(settings.web_embedding_provider, "openai")
        self.assertEqual(settings.web_embedding_model, "text-embedding-test")

    def test_model_use_updates_default_model_in_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text(
                "DEEPSEEK_PROVIDER=deepseek\n"
                "DEEPSEEK_MODEL=deepseek-chat\n"
                "XIAOMIMIMO_PROVIDER=mimo\n"
                "XIAOMIMIMO_MODEL=MiMo-72B-A27B\n"
                "DEFAULT_MODEL=DEEPSEEK\n"
                "DEFAULT_LIGHT_MODEL=DEEPSEEK\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=root):
                self.assertEqual(main(["model", "use", "XIAOMIMIMO", "--target", "light"]), 0)

            text = env_path.read_text(encoding="utf-8")

        self.assertIn("DEFAULT_MODEL=DEEPSEEK", text)
        self.assertIn("DEFAULT_LIGHT_MODEL=XIAOMIMIMO", text)


if __name__ == "__main__":
    unittest.main()
