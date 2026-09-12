# PGDesk implementation plan

## Outcome
Standalone personal repository using Oracle manual execution's Textual/Rich libraries and keyboard-first philosophy. Multiple independent cluster/database tabs, resilient pools, schema/tables/views navigation, equal toggleable AI/editor/results panels, SQL highlighting, CSV export, direct schema-aware OpenAI assistance and settings.

## Scope and design
No BLP source changes, board issue, remote repository publication, database writes or deployments. The user authorizes read-only AWS/database connection verification. Product requirements are supplied by the user; no additional product design stage is needed. No business schema is created or migrated.

- `config.py`: local dotenv cluster configuration; credentials referenced by explicit AWS profile/region/secret, libpq service or environment variable. Mutable AI preferences stay in a separate local user file, never a credential store.
- `database.py`: `DatabaseSession(cluster, database)` owns a fixed warm Psycopg pool, health thread, active query and counters. `catalog()` returns metadata; `execute(sql, read_only=True)` returns a bounded result or raises; `cancel()` signals only the current execution; `close()` cancels work, stops maintenance and closes the pool. Calls are synchronous and run off the UI thread. Pools never replay SQL. Connection acquisition and operations have deadlines; background reconnection continues until tab closure.
- `catalog.py`: metadata SQL and immutable schema/relation/column descriptions; PostgreSQL catalogs are the source of truth. Full accessible relation metadata is sent with each AI request; no row values, credentials or previous query outputs.
- `ai.py`: `SqlAssistant.suggest(settings, catalog, history, message)` calls OpenAI Responses directly, returning SQL for explicit insertion into the editor. No tools or autonomous execution. Per-tab conversations stay in memory. Model/settings failures are surfaced without model substitution.
- `screens.py`: searchable cluster/database chooser, first-run configuration guidance, settings, confirmation, CSV path and help dialogs. All focusable and keyboard reachable.
- `workspace.py`: one schema tree/editor/result/chat state owner per tab. Closing a tab cancels its tasks before destroying database resources. Responses target the owning tab, never whichever happens to be active later.
- `app.py`: top-level tabs, status, navigation and panel actions; no database implementation.

## Contracts and safety
- Each Run is one SQL statement in one transaction: success commits, failure rolls back. Explicit transaction-control, SET/RESET and COPY commands are rejected; no persistent transaction/session promise across pooled connections. Read-only by default, deliberate write-mode opt-in. No automatic query retry, including uncertain commit outcomes.
- Result display is bounded to configured rows; libpq still receives the full result, so use SQL LIMIT for large queries. Truncation is explicit and CSV exports exactly the retained result with a truncation warning. File creation is exclusive and owner-only (0600), never silent overwrite. Grid cell previews are capped at 2,000 characters; CSV retains complete cell values.
- Two warm connections per open tab; connection maximum explicit. Health checks, checkout validation, socket keepalives and connect/statement timeouts; pool worker retries with backoff, maintenance restarts exhausted reconnect cycles. Status reports measured connection count/age/errors and application payload estimates, not fabricated wire-byte counters.
- Schema metadata is untrusted data in AI context. Default `gpt-5.6-terra`, medium reasoning, Fast mode (`service_tier=fast`); editable model string and reasoning selector. `store=false`; no query data included. Settings explain paid fast tier and schema disclosure.
- `.env` config: comma-separated cluster identifiers select uppercase PGDESK_<ID>_* sections with unique labels, exactly one credential source, maintenance database, optional database allowlist and production marker. PGDESK_OPENAI_* stores the AI secret reference and dot-separated key path. No embedded secrets or implicit shell interpolation/overrides. Local AI settings retain model, reasoning, fast and system_prompt.

## Ordered working slices
1. Package/config + real database connection, catalog, query, cancellation and recovery contract.
2. Actual Textual multi-tab workflows, editor/results/tree and CSV.
3. Direct OpenAI integration and keyboard settings; actual-path verification of full interface.

## Verification
Use a disposable local PostgreSQL 15 container with synthetic schema/data, not business inputs. Exercise reuse, termination/restart recovery past reconnect timeout, cancellation, rollback, read-only enforcement, tab isolation, result truncation and CSV roundtrip. Run the real TUI in a PTY and inspect output/interaction, complemented by throwaway Textual Pilot smokes and rendered screenshots. Retain only uncertain database/config/CSV contract tests. OpenAI live smoke requires an API key; absent credentials are disclosed, never replaced with a fake live claim. Review final diff and run Ruff/pytest against this new repo.

## Source facts
Oracle reference: `blp-emsx/scripts/oracle_manual_execution/tui.py` and its launcher; Textual/Rich, bracket tab switching, slash filtering and Escape input cancellation. OpenAI primary docs confirm `gpt-5.6-terra` and `service_tier=fast`. Psycopg documentation establishes pool check/reset/reconnect behavior; installed implementation and local failure smoke are the recovery oracle.

## Initial implementation verification
- Runtime: CPython 3.13, locked dependencies in `uv.lock`.
- `uv run ruff check src tests` and `uv run ruff format --check src tests`: passed.
- `PGDESK_TEST_DSN=<disposable loopback PostgreSQL DSN> uv run pytest -q`: 17 passed. Covers config ambiguity/credential references, atomic settings, private/exclusive CSV and quoted identifiers; real PostgreSQL readonly enforcement, failed-write rollback, session reset, empty/truncated RETURNING results, cancellation, killed-backend replacement, catalog constraints/views and pooled transaction restrictions. Includes the observed tab-close/app-exit race: all shutdown callers now await one shielded cleanup, even when one caller is cancelled; no return with live pool connections.
- Actual application smoke: four chooser-opened workspaces (`test-a/domain1`, `prod-a/domain1`, `test-a/domain2`, `prod-a/domain2`), eight connections, two synthetic domains with 20 tables each plus views/empty schemas. Passed keyboard chooser/tab switching, slash-filtered tree selection, SQL highlighting/execution, result navigation, CSV, all 0/1/2/3 panel combinations, four AI settings, write confirmation, SQL errors, cancellation, tab-local state and complete pool cleanup.
- Actual CLI launched in a PTY: welcome, Ctrl+N chooser, database selection, connected READY workspace, F1 help and Ctrl+Q clean exit 0 observed. Rendered workspace and settings screenshots visually inspected.
- Whole-server outage smoke: two live connections; stopped PostgreSQL beyond a 1-second reconnect window; observed RECONNECTING; restarted server; recovered two live/two idle connections without any user query; subsequent SELECT succeeded; close left zero connections. First fixture used a Docker ephemeral port that changed across restart; repeated with a fixed loopback endpoint and passed.
- OpenAI: actual AsyncOpenAI Responses SDK exercised with a local HTTP protocol substitute, including full schema metadata, model/reasoning/Fast parameters, no result-row/credential leakage, per-tab history and explicit draft insertion. No live OpenAI response claimed: `OPENAI_API_KEY` was absent.
- `uv build`: built source distribution and wheel successfully.
- At initial delivery, real cluster authentication, remote TLS/VPN and live OpenAI requests remained unexercised. The AWS follow-up below supersedes those credential and verification gaps.
- Local review scope: all new sources/config/tests against the user requirements and safety contract. No configured specialist delegates were available; review was local, not independent. No remote repository was created or pushed.

## Running
From this repository: `uv run pgdesk`. Edit `.env` for cluster names, AWS profiles, regions and secret names; `.env.example` documents every option. Or pass `--config /path/to/.env`. Values remain in Secrets Manager or a separately supplied process environment. AWS RDS connections verify TLS against the standard libpq CA trust location. F1 contains the complete keyboard and execution reference; F9 configures AI preferences.

## AWS credential configuration follow-up
User authorizes using the miscellaneous Slack application's OpenAI key and test/production RDS administrator secrets through named local AWS profiles. Discovery confirmed the key is at `slack_agent.openai_api_key`, not `slack_app`, and both database secrets use the standard RDS JSON shape. Account-specific references live only in the ignored `.env`.

- Add a small `aws_secrets.py` adapter around the installed AWS CLI. Config stores only explicit profile/region/secret references and an OpenAI key path. Secret values remain in memory, never in config, diagnostics, arguments or repository artifacts.
- `Cluster.connection_kwargs()` resolves RDS JSON fields on physical connection creation so reconnects obtain current credentials. AWS-backed PostgreSQL uses verified TLS with libpq's existing trust configuration; service/DSN behavior remains unchanged.
- `SqlAssistant` resolves the configured key off the UI thread, serializes lazy client initialization, and retains the key only in the OpenAI client. Explicit AWS configuration takes precedence over ambient OPENAI_API_KEY; AWS failures never silently fall back to another credential.
- User clarification makes the repository's ignored `.env` the editable configuration source for `test` and `prod`; remove the obsolete TOML loader, example and task-created local TOML file. Keep the production marker and existing server-enforced read-only default. No database writes or infrastructure mutations are authorized.
- Verification: sanitized live secret shape inspection; actual test/prod database discovery, warm pools and read-only constant queries; direct OpenAI request with synthetic schema only; keyboard chooser smoke; isolated secret-error/config/initialization edge tests and existing regression suite. No production schema or row data is sent to OpenAI by the smoke.

### Follow-up verification results
- `uv run ruff check src tests`: passed; `uv build`: source distribution and wheel built. `PGDESK_TEST_DSN=<disposable loopback PostgreSQL 15 DSN> uv run pytest -q`: 25 passed, including secret-payload suppression, credential-source precedence, concurrent client cleanup and dotenv environment isolation.
- Final `.env` used for real database discovery: 36 test databases, 18 production databases. Both sessions warmed two connections and passed constant queries with `transaction_read_only=on` and verified TLS. No business rows read or database writes performed.
- Actual TUI keyboard smoke opened both configured `postgres` workspaces, preloaded catalogs, ran read-only constants, opened settings and closed both pools. Rendered screenshot visually inspected. Actual CLI launched without `--config`, loaded the repository `.env`, reached test/postgres READY and exited cleanly with Ctrl+Q (exit 0).
- Live OpenAI Responses request through `.env` and `miscellaneous` succeeded with synthetic schema only, using `gpt-5.6-terra` and Fast mode; returned service tier was `priority`. No production schema or data sent to OpenAI.
- Old TOML configuration removed; `.env.example` documents the sole supported cluster configuration format. Existing tabs retain their connection reference until reopened; restart after changing OpenAI authentication references.
