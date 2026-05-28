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
    PRIMARY KEY (ts_code, trade_date)
);

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

CREATE TABLE IF NOT EXISTS stock_minute (
    ts_code TEXT NOT NULL,
    period TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    trade_time TEXT NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    data_source TEXT,
    PRIMARY KEY (ts_code, period, trade_time)
);

CREATE TABLE IF NOT EXISTS industry_daily (
    index_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close DOUBLE,
    PRIMARY KEY (index_code, trade_date)
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

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT NOT NULL,
    model_name TEXT,
    summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id)
);

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
