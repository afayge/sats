from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sats.llm.model_config import resolve_model_selection

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    load_dotenv = None  # type: ignore[assignment]


DEFAULT_ENV_CONTENT = """# SATS local configuration
SATS_DB_PATH=data/sats.duckdb

# Market data
TUSHARE_TOKEN=
TUSHARE_TIMEOUT_SECONDS=30
TUSHARE_MAX_RETRIES=2
TICKFLOW_API_KEY=
TICKFLOW_BASE_URL=https://api.tickflow.org
TICKFLOW_TIMEOUT_SECONDS=30
TICKFLOW_MAX_RETRIES=3
WEB_SEARCH_TIMEOUT_SECONDS=10
WEB_SEARCH_CACHE_TTL_SECONDS=43200
SOCIAL_HOT_CACHE_TTL_SECONDS=300
WEB_SEARCH_MAX_RESULTS=10
WEB_SEARCH_BACKEND=auto
WEB_SEARCH_PROVIDERS=anysearch,ddgs,bing
ANYSEARCH_API_KEY=
WEB_PAGE_CACHE_TTL_SECONDS=86400
WEB_RESPONSES_BASE_URL=
WEB_RESPONSES_API_KEY=
WEB_RESPONSES_MODEL=
WEB_SEARCH_CONTEXT_SIZE=auto
WEB_TAVILY_API_KEY=
WEB_BOCHA_API_KEY=
WEB_QUERIT_API_KEY=
WEB_EMBEDDING_PROVIDER=auto
WEB_EMBEDDING_BASE_URL=
WEB_EMBEDDING_API_KEY=
WEB_EMBEDDING_MODEL=
WEB_FASTEMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

# Iwencai SkillHub
IWENCAI_BASE_URL=https://openapi.iwencai.com
IWENCAI_API_KEY=
IWENCAI_SKILLHUB_CLI=iwencai-skillhub-cli

# LLM model profiles
DEEPSEEK_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_LIGHT_MODEL=deepseek-chat

XIAOMIMIMO_PROVIDER=mimo
XIAOMIMIMO_BASE_URL=https://api.xiaomimimo.com/v1
XIAOMIMIMO_API_KEY=
XIAOMIMIMO_MODEL=MiMo-72B-A27B
XIAOMIMIMO_LIGHT_MODEL=MiMo-72B-A27B

DEFAULT_MODEL=DEEPSEEK
DEFAULT_LIGHT_MODEL=XIAOMIMIMO
LLM_TEMPERATURE=0.0
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=2
# LLM_REASONING_EFFORT=medium

# Trading
TRADING_MODE=paper
MINIQMT_GATEWAY_URL=
MINIQMT_GATEWAY_TOKEN=
REQUIRE_TRADE_CONFIRMATION=true
SATS_BROKER_PROVIDER=
SATS_QMT_BRIDGE_URL=
SATS_QMT_TOKEN=
SATS_QMT_ACCOUNT_ID=
SATS_QMT_ACCOUNT_TYPE=STOCK
SATS_QMT_USERDATA_PATH=
SATS_QMT_SESSION_ID=

# Controlled self-diagnosis and repair
SATS_SELF_REPAIR_MODE=propose
SATS_SELF_REPAIR_MAX_ATTEMPTS=2
SATS_SELF_REPAIR_TIMEOUT_SECONDS=120
SATS_SELF_REPAIR_TEST_TIMEOUT_SECONDS=300
"""


@dataclass(frozen=True)
class Settings:
    project_root: Path
    env_path: Path
    db_path: Path
    tushare_token: str
    tushare_timeout_seconds: int
    tushare_max_retries: int
    tickflow_api_key: str
    tickflow_base_url: str
    tickflow_timeout_seconds: int
    tickflow_max_retries: int
    web_search_timeout_seconds: int
    web_search_cache_ttl_seconds: int
    social_hot_cache_ttl_seconds: int
    web_search_max_results: int
    llm_provider: str
    llm_profile: str
    light_llm_provider: str
    light_llm_profile: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    light_model_name: str
    llm_timeout_seconds: int
    llm_max_retries: int
    trading_mode: str
    miniqmt_gateway_url: str
    miniqmt_gateway_token: str
    require_trade_confirmation: bool
    broker_provider: str
    qmt_bridge_url: str
    qmt_token: str
    qmt_account_id: str
    qmt_account_type: str
    qmt_userdata_path: str
    qmt_session_id: str
    web_search_backend: str = "auto"
    web_responses_base_url: str = ""
    web_responses_api_key: str = ""
    web_responses_model: str = ""
    web_search_context_size: str = "auto"
    web_search_providers: str = "anysearch,ddgs,bing"
    anysearch_api_key: str = ""
    web_page_cache_ttl_seconds: int = 86400
    web_tavily_api_key: str = ""
    web_bocha_api_key: str = ""
    web_querit_api_key: str = ""
    web_embedding_provider: str = "auto"
    web_embedding_base_url: str = ""
    web_embedding_api_key: str = ""
    web_embedding_model: str = ""
    web_fastembed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    iwencai_base_url: str = "https://openapi.iwencai.com"
    iwencai_api_key: str = ""
    iwencai_skillhub_cli: str = "iwencai-skillhub-cli"
    self_repair_mode: str = "propose"
    self_repair_max_attempts: int = 2
    self_repair_timeout_seconds: int = 120
    self_repair_test_timeout_seconds: int = 300


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in choices else default


def load_settings(project_root: Path | None = None, env_path: Path | None = None) -> Settings:
    root = _resolve_project_root(project_root, env_path)
    env_file = (env_path or root / ".env").resolve()
    if load_dotenv is not None and env_file.exists():
        load_dotenv(env_file, override=True)

    db_path = Path(os.getenv("SATS_DB_PATH", "data/sats.duckdb"))
    if not db_path.is_absolute():
        db_path = root / db_path
    main_model = resolve_model_selection(profile="default")
    light_model = resolve_model_selection(profile="light")

    return Settings(
        project_root=root,
        env_path=env_file,
        db_path=db_path,
        tushare_token=os.getenv("TUSHARE_TOKEN", "").strip(),
        tushare_timeout_seconds=_env_int("TUSHARE_TIMEOUT_SECONDS", 30),
        tushare_max_retries=_env_int("TUSHARE_MAX_RETRIES", 2),
        tickflow_api_key=os.getenv("TICKFLOW_API_KEY", "").strip(),
        tickflow_base_url=os.getenv("TICKFLOW_BASE_URL", "https://api.tickflow.org").strip(),
        tickflow_timeout_seconds=_env_int("TICKFLOW_TIMEOUT_SECONDS", 30),
        tickflow_max_retries=_env_int("TICKFLOW_MAX_RETRIES", 3),
        web_search_timeout_seconds=_env_int("WEB_SEARCH_TIMEOUT_SECONDS", 10),
        web_search_cache_ttl_seconds=_env_int("WEB_SEARCH_CACHE_TTL_SECONDS", 43200),
        social_hot_cache_ttl_seconds=_env_int("SOCIAL_HOT_CACHE_TTL_SECONDS", 300),
        web_search_max_results=_env_int("WEB_SEARCH_MAX_RESULTS", 10),
        web_search_backend=_env_choice("WEB_SEARCH_BACKEND", "auto", {"auto", "rag", "responses", "ddgs"}),
        web_search_providers=os.getenv("WEB_SEARCH_PROVIDERS", "anysearch,ddgs,bing").strip(),
        anysearch_api_key=os.getenv("ANYSEARCH_API_KEY", "").strip(),
        web_page_cache_ttl_seconds=_env_int("WEB_PAGE_CACHE_TTL_SECONDS", 86400),
        web_responses_base_url=os.getenv("WEB_RESPONSES_BASE_URL", "").strip(),
        web_responses_api_key=os.getenv("WEB_RESPONSES_API_KEY", "").strip(),
        web_responses_model=os.getenv("WEB_RESPONSES_MODEL", "").strip(),
        web_search_context_size=_env_choice("WEB_SEARCH_CONTEXT_SIZE", "auto", {"auto", "medium", "high"}),
        web_tavily_api_key=os.getenv("WEB_TAVILY_API_KEY", "").strip(),
        web_bocha_api_key=os.getenv("WEB_BOCHA_API_KEY", "").strip(),
        web_querit_api_key=os.getenv("WEB_QUERIT_API_KEY", "").strip(),
        web_embedding_provider=_env_choice("WEB_EMBEDDING_PROVIDER", "auto", {"auto", "openai", "fastembed", "none"}),
        web_embedding_base_url=os.getenv("WEB_EMBEDDING_BASE_URL", "").strip(),
        web_embedding_api_key=os.getenv("WEB_EMBEDDING_API_KEY", "").strip(),
        web_embedding_model=os.getenv("WEB_EMBEDDING_MODEL", "").strip(),
        web_fastembed_model=os.getenv(
            "WEB_FASTEMBED_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ).strip(),
        iwencai_base_url=os.getenv("IWENCAI_BASE_URL", "https://openapi.iwencai.com").strip(),
        iwencai_api_key=os.getenv("IWENCAI_API_KEY", "").strip(),
        iwencai_skillhub_cli=os.getenv("IWENCAI_SKILLHUB_CLI", "iwencai-skillhub-cli").strip(),
        llm_provider=main_model.provider,
        llm_profile=main_model.profile_name,
        light_llm_provider=light_model.provider,
        light_llm_profile=light_model.profile_name,
        openai_api_key=main_model.api_key,
        openai_base_url=main_model.base_url,
        openai_model=main_model.model,
        light_model_name=light_model.model,
        llm_timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 120),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 2),
        trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
        miniqmt_gateway_url=os.getenv("MINIQMT_GATEWAY_URL", "").strip(),
        miniqmt_gateway_token=os.getenv("MINIQMT_GATEWAY_TOKEN", "").strip(),
        require_trade_confirmation=_env_bool("REQUIRE_TRADE_CONFIRMATION", True),
        broker_provider=os.getenv("SATS_BROKER_PROVIDER", "").strip().lower(),
        qmt_bridge_url=(os.getenv("SATS_QMT_BRIDGE_URL") or os.getenv("MINIQMT_GATEWAY_URL", "")).strip(),
        qmt_token=(os.getenv("SATS_QMT_TOKEN") or os.getenv("MINIQMT_GATEWAY_TOKEN", "")).strip(),
        qmt_account_id=os.getenv("SATS_QMT_ACCOUNT_ID", "").strip(),
        qmt_account_type=os.getenv("SATS_QMT_ACCOUNT_TYPE", "STOCK").strip().upper(),
        qmt_userdata_path=os.getenv("SATS_QMT_USERDATA_PATH", "").strip(),
        qmt_session_id=os.getenv("SATS_QMT_SESSION_ID", "").strip(),
        self_repair_mode=_env_choice("SATS_SELF_REPAIR_MODE", "propose", {"off", "runtime", "propose"}),
        self_repair_max_attempts=max(0, _env_int("SATS_SELF_REPAIR_MAX_ATTEMPTS", 2)),
        self_repair_timeout_seconds=max(1, _env_int("SATS_SELF_REPAIR_TIMEOUT_SECONDS", 120)),
        self_repair_test_timeout_seconds=max(1, _env_int("SATS_SELF_REPAIR_TEST_TIMEOUT_SECONDS", 300)),
    )


def _resolve_project_root(project_root: Path | None = None, env_path: Path | None = None) -> Path:
    if project_root is not None:
        return project_root.resolve()
    if env_path is not None:
        return env_path.resolve().parent
    cwd = Path.cwd().resolve()
    if (cwd / ".env").exists():
        return cwd
    return Path(__file__).resolve().parents[1]


def init_env_file(path: Path, *, overwrite: bool = False) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_ENV_CONTENT, encoding="utf-8")
    return True
