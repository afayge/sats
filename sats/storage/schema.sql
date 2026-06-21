CREATE TABLE IF NOT EXISTS stock_daily (
    ts_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    pct_chg DOUBLE,
    data_source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date)
);

ALTER TABLE stock_daily ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE stock_daily ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS stock_daily_basic (
    ts_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    turnover_rate DOUBLE,
    turnover_rate_f DOUBLE,
    circ_mv DOUBLE,
    float_share DOUBLE,
    free_share DOUBLE,
    float_mv DOUBLE,
    total_mv DOUBLE,
    pe DOUBLE,
    pb DOUBLE,
    ps DOUBLE,
    data_source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date)
);

ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS turnover_rate_f DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS float_share DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS free_share DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS float_mv DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS total_mv DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS pe DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS pb DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS ps DOUBLE;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE stock_daily_basic ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    industry TEXT,
    market TEXT,
    exchange TEXT,
    list_date TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code)
);

DROP TABLE IF EXISTS stock_minute;

CREATE TABLE IF NOT EXISTS industry_daily (
    index_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close DOUBLE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    pct_chg DOUBLE,
    data_source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_code, trade_date)
);

ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS open DOUBLE;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS high DOUBLE;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS low DOUBLE;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS vol DOUBLE;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS amount DOUBLE;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS pct_chg DOUBLE;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE industry_daily ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS stock_minute_cache (
    ts_code TEXT NOT NULL,
    period TEXT NOT NULL,
    datetime TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    data_source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, period, datetime)
);

CREATE TABLE IF NOT EXISTS realtime_quote_cache (
    ts_code TEXT NOT NULL,
    as_of_time TEXT NOT NULL,
    price DOUBLE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    pre_close DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    pct_chg DOUBLE,
    data_source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code)
);

CREATE TABLE IF NOT EXISTS sector_basic (
    sector_code TEXT NOT NULL,
    name TEXT,
    sector_type TEXT,
    exchange TEXT,
    list_date TEXT,
    data_source TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sector_code)
);

CREATE TABLE IF NOT EXISTS sector_daily (
    sector_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    pct_chg DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    data_source TEXT,
    PRIMARY KEY (sector_code, trade_date)
);

CREATE TABLE IF NOT EXISTS sector_members (
    sector_code TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    name TEXT,
    weight DOUBLE,
    in_date TEXT,
    out_date TEXT,
    is_new BOOLEAN,
    data_source TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sector_code, ts_code)
);

CREATE TABLE IF NOT EXISTS stock_moneyflow (
    ts_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    main_net_amount DOUBLE,
    data_source TEXT,
    PRIMARY KEY (ts_code, trade_date)
);

CREATE TABLE IF NOT EXISTS stock_fundamentals (
    ts_code TEXT NOT NULL,
    end_date TEXT NOT NULL,
    ann_date TEXT,
    total_revenue DOUBLE,
    revenue DOUBLE,
    net_profit DOUBLE,
    profit DOUBLE,
    roe DOUBLE,
    debt_to_assets DOUBLE,
    total_assets DOUBLE,
    total_liab DOUBLE,
    data_source TEXT,
    PRIMARY KEY (ts_code, end_date)
);

CREATE TABLE IF NOT EXISTS screening_results (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    score DOUBLE NOT NULL,
    matched_conditions TEXT NOT NULL,
    failed_conditions TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code, rule_name)
);

CREATE TABLE IF NOT EXISTS factor_runs (
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    universe TEXT NOT NULL DEFAULT '',
    factor_ids_json TEXT NOT NULL DEFAULT '[]',
    params_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    report_path TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id)
);

CREATE TABLE IF NOT EXISTS factor_candidates (
    run_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score DOUBLE NOT NULL,
    factors_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, ts_code)
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT NOT NULL,
    title TEXT,
    model_name TEXT,
    summary TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    last_read_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id)
);

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS meta_json TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS archived BOOLEAN;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS pinned BOOLEAN;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS last_read_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    parent_id TEXT,
    model_id TEXT,
    sources_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'done',
    error_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id)
);

ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS parent_id TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sources_json TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS files_json TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS usage_json TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS error_json TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_message_id TEXT,
    assistant_message_id TEXT,
    request TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    symbols_json TEXT NOT NULL DEFAULT '[]',
    trade_date TEXT,
    data_names_json TEXT NOT NULL DEFAULT '[]',
    skill_names_json TEXT NOT NULL DEFAULT '[]',
    model_name TEXT,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds DOUBLE,
    error_json TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (turn_id)
);

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS user_message_id TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS assistant_message_id TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS intent TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS symbols_json TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS trade_date TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS data_names_json TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS skill_names_json TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS tool_call_count INTEGER;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS error_json TEXT;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS meta_json TEXT;

CREATE TABLE IF NOT EXISTS chat_turn_events (
    event_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    item_type TEXT NOT NULL DEFAULT '',
    item_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    content TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds DOUBLE,
    PRIMARY KEY (event_id)
);

ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS seq INTEGER;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS item_type TEXT;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS item_name TEXT;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS content TEXT;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS payload_json TEXT;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
ALTER TABLE chat_turn_events ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE;

CREATE TABLE IF NOT EXISTS chat_turn_items (
    item_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    artifact_paths_json TEXT NOT NULL DEFAULT '[]',
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds DOUBLE,
    PRIMARY KEY (item_id)
);

ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS seq INTEGER;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS item_type TEXT;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS item_name TEXT;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS input_json TEXT;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS output_json TEXT;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS artifact_paths_json TEXT;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
ALTER TABLE chat_turn_items ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE;

CREATE TABLE IF NOT EXISTS chat_artifacts (
    artifact_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artifact_id)
);

ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS path TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS mime_type TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS meta_json TEXT;
ALTER TABLE chat_artifacts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS chat_pending_actions (
    action_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (action_id)
);

ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS action_type TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS payload_json TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS result_json TEXT;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE chat_pending_actions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS chat_memories (
    memory_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    importance DOUBLE NOT NULL DEFAULT 0.5,
    source_session_id TEXT,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    PRIMARY KEY (memory_id)
);

CREATE TABLE IF NOT EXISTS interaction_history (
    history_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    request TEXT NOT NULL,
    source TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    duration_seconds DOUBLE,
    report_path TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    deleted_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (history_id)
);

ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS request TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS output TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS report_path TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS meta_json TEXT;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
ALTER TABLE interaction_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS knowledge_bases (
    knowledge_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    collection_name TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (knowledge_id),
    UNIQUE (name),
    UNIQUE (collection_name)
);

CREATE TABLE IF NOT EXISTS knowledge_files (
    file_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    size_bytes BIGINT NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (file_id),
    UNIQUE (content_hash, path)
);

CREATE TABLE IF NOT EXISTS knowledge_file_links (
    knowledge_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (knowledge_id, file_id)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT NOT NULL,
    knowledge_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    page_number INTEGER,
    line_start INTEGER,
    line_end INTEGER,
    tags_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id),
    UNIQUE (knowledge_id, file_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_collection ON knowledge_chunks(collection_name);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_knowledge ON knowledge_chunks(knowledge_id);

CREATE TABLE IF NOT EXISTS web_documents (
    document_id TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    extraction_method TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMP,
    fetched_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (document_id),
    UNIQUE (canonical_url)
);

CREATE TABLE IF NOT EXISTS web_chunks (
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id),
    UNIQUE (document_id, chunk_index, content_hash)
);

CREATE TABLE IF NOT EXISTS web_chunk_embeddings (
    chunk_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    vector DOUBLE[] NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id, provider, model)
);

CREATE INDEX IF NOT EXISTS idx_web_documents_expires ON web_documents(expires_at);
CREATE INDEX IF NOT EXISTS idx_web_chunks_document ON web_chunks(document_id);

CREATE TABLE IF NOT EXISTS monitor_positions (
    ts_code TEXT NOT NULL,
    name TEXT,
    quantity DOUBLE,
    buy_price DOUBLE,
    buy_date TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code)
);

CREATE TABLE IF NOT EXISTS monitor_watchlist (
    ts_code TEXT NOT NULL,
    name TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code)
);

CREATE TABLE IF NOT EXISTS monitor_buy_candidates (
    ts_code TEXT NOT NULL,
    name TEXT,
    source_event_id TEXT,
    rule_name TEXT,
    signal_name TEXT,
    signal_label TEXT,
    score DOUBLE,
    price DOUBLE,
    reason TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code)
);

CREATE TABLE IF NOT EXISTS monitor_events (
    event_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    name TEXT,
    source_list TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    signal_label TEXT NOT NULL,
    side TEXT NOT NULL,
    score DOUBLE,
    price DOUBLE,
    trade_time TEXT,
    message TEXT NOT NULL,
    watch_levels_json TEXT NOT NULL DEFAULT '{}',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id)
);

CREATE TABLE IF NOT EXISTS monitor_trade_events (
    trade_event_id TEXT NOT NULL,
    event_id TEXT,
    ts_code TEXT NOT NULL,
    name TEXT,
    action TEXT NOT NULL,
    side TEXT NOT NULL,
    price DOUBLE,
    quantity DOUBLE,
    status TEXT NOT NULL,
    message TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_event_id)
);

CREATE TABLE IF NOT EXISTS monitor_runtime (
    service_name TEXT NOT NULL,
    status TEXT NOT NULL,
    pid BIGINT,
    heartbeat_at TIMESTAMP,
    started_at TIMESTAMP,
    stopped_at TIMESTAMP,
    params_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (service_name)
);

CREATE TABLE IF NOT EXISTS monitor_plans (
    plan_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    active_windows_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id)
);

CREATE TABLE IF NOT EXISTS monitor_plan_items (
    item_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    name TEXT,
    summary TEXT,
    risk_note TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_id)
);

CREATE TABLE IF NOT EXISTS monitor_plan_trigger_groups (
    group_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    message TEXT,
    conditions_json TEXT NOT NULL DEFAULT '[]',
    sizing_json TEXT NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id)
);

CREATE TABLE IF NOT EXISTS monitor_plan_trigger_state (
    group_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    last_result TEXT NOT NULL DEFAULT 'unknown',
    crossing_count INTEGER NOT NULL DEFAULT 0,
    notification_count INTEGER NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    last_values_json TEXT NOT NULL DEFAULT '[]',
    last_evaluated_at TIMESTAMP,
    last_triggered_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, trade_date)
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'STOCK',
    cash DOUBLE,
    available_cash DOUBLE,
    market_value DOUBLE,
    total_asset DOUBLE,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, account_id)
);

CREATE TABLE IF NOT EXISTS broker_positions (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    name TEXT,
    quantity DOUBLE,
    available_quantity DOUBLE,
    cost_price DOUBLE,
    price DOUBLE,
    market_value DOUBLE,
    pnl DOUBLE,
    pnl_pct DOUBLE,
    source TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, account_id, ts_code)
);

CREATE TABLE IF NOT EXISTS broker_orders (
    sats_order_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    broker_order_id TEXT,
    ts_code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity DOUBLE,
    price DOUBLE,
    price_type TEXT,
    status TEXT NOT NULL,
    message TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sats_order_id)
);

CREATE TABLE IF NOT EXISTS broker_trades (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    broker_order_id TEXT,
    ts_code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity DOUBLE,
    price DOUBLE,
    trade_time TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, account_id, trade_id)
);

CREATE TABLE IF NOT EXISTS broker_order_events (
    event_id TEXT NOT NULL,
    sats_order_id TEXT,
    broker_order_id TEXT,
    provider TEXT NOT NULL,
    account_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id)
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    text TEXT NOT NULL,
    schedule_kind TEXT NOT NULL,
    days_json TEXT NOT NULL DEFAULT '[]',
    time_of_day TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMP,
    last_run_at TIMESTAMP,
    last_status TEXT,
    running BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    run_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    text TEXT NOT NULL,
    scheduled_for TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT NOT NULL,
    duration_seconds DOUBLE,
    output_text TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    report_path TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id)
);
