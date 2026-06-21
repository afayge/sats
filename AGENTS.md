# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

## 5. SATS Project Structure

Use the current module boundaries when adding or changing features:

- `sats/cli.py`: argparse one-shot CLI entrypoint. Both `python -m sats ...` and `sats ...` reuse this command registry and `cmd_*` implementations.
- `sats/repl.py`: Codex-style interactive CLI. It owns slash-command allowlisting, completion words, `/help` examples, and conversion from `/command ...` to existing CLI argv.
- `sats/api/app.py`: FastAPI application and HTTP API routes.
- `sats/config.py`: `.env` loading, settings, and init template.
- `sats/data/`: market data providers, currently Tushare, TickFlow, and optional AkShare supplements.
- `sats/screening/`: screening interfaces, rule registry, rule implementations, and screening service.
- `sats/storage/`: DuckDB schema and storage access layer.
- `sats/llm/`: LLM provider abstraction and helpers.
- `sats/analysis/`: post-screening analysis bridges and LLM review flows.
- `tests/`: CLI, REPL, API, provider, rule, storage, LLM, and analysis coverage.

## 6. User-Facing Feature Checklist

Any new user-callable SATS feature must keep all command surfaces in sync:

- Register an argparse subcommand and `cmd_*` handler in `sats/cli.py`.
- Confirm the command works through `python -m sats <command> ...`.
- Confirm the command works through the console script form `sats <command> ...`.
- Add the slash command to `sats/repl.py` (`CLI_COMMANDS`, relevant completion words, and `/help` examples when useful).
- Update `README.md` with the feature description, parameters, and command examples.
- Add or update tests for both one-shot CLI dispatch and REPL slash-command conversion.
- If the feature accepts A-share stock codes from users, normalize them at the input boundary with `sats.symbols`; do not hand-roll suffix inference.
- If the feature needs A-share market data, request it through `sats.data.astock_provider.AStockDataProvider`; do not import or instantiate TickFlow, Tushare, AkShare, news, or future A-share backend providers directly from business modules. Backend providers stay behind `sats/data/` adapters and provider-specific tests.
- If the feature performs DSA-style stock analysis, reuse the native SATS DSA service instead of shelling out to external `daily_stock_analysis`.
- If the feature performs long-running data fetch, calculation, LLM, report, or monitoring work, expose coarse step progress through `sats.progress`; keep progress silent for non-TTY, tests, pipes, and JSON output. Do not hand-roll separate progress panels.
- If the feature performs scheduled execution, route it through `sats.scheduler` and SATS internal CLI/chat entrypoints; do not execute arbitrary shell commands from scheduler tasks.
- If the feature creates AI-generated screening rules, route it through `sats.screening.rule_composer`, write only to `sats/screening/rules/generated/`, require explicit user confirmation, and do not add arbitrary subprocess or shell-based code generation.

FastAPI routes are added only when the feature is intended to be available over HTTP; the CLI, slash command, and README requirements apply to every user-callable command-line feature.

## 7. Command Output Naming Constraint

All user-facing command results must display a security name whenever they display a security code:

- An A-share stock code must be accompanied by its stock name.
- An index code must be accompanied by its index name.
- This applies to one-shot CLI and REPL slash-command output, including text tables, Markdown, and JSON.
- Resolve names through the local `stock_basic` cache and `AStockDataProvider`; do not call backend providers directly.
