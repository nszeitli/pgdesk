# PGDesk implementation plan

## Outcome
Standalone personal repository using Oracle manual execution's Textual/Rich libraries and keyboard-first philosophy. Multiple independent cluster/database tabs, resilient pools, schema/tables/views navigation, equal toggleable AI/editor/results panels, SQL highlighting, CSV export, direct schema-aware OpenAI assistance and settings.

## Scope and design
No BLP source changes, board issue, remote repository publication, database writes or deployments. The user authorizes read-only AWS/database connection verification. Product requirements are supplied by the user; no additional product design stage is needed. No business schema is created or migrated.

- `config.py`: local dotenv cluster configuration; credentials referenced by explicit AWS profile/region/secret, libpq service or environment variable. Mutable AI preferences stay in a separate local user file, never a credential store.
- `database.py`: `DatabaseSession(cluster, database)` owns a fixed warm Psycopg pool, health thread, active query and counters. `catalog()` returns metadata; `execute(sql, read_only=True)` returns a bounded result or raises; `cancel()` signals only the current execution; `close()` cancels work, stops maintenance and closes the pool. Calls are synchronous and run off the UI thread. Pools never replay SQL. Connection acquisition and operations have deadlines; background reconnection continues until tab closure.
- `catalog.py`: metadata SQL and immutable schema/relation/column descriptions; PostgreSQL catalogs are the source of truth. Accessible relation metadata is sent with AI requests, alongside operator-authored prompt/SQL context for interactive prompting; no automatic query results or configured credentials are sent.
- `ai.py`: `SqlAssistant.suggest(settings, catalog, history, message)` calls OpenAI Responses directly, returning one validated SQL statement for insertion into the editor. No tools; prompt replies never execute. First-table browsing separately restricts an inferred recipe before a bounded automatic read. Per-tab prompt context stays in memory. Model/settings failures are surfaced without model substitution.
- `screens.py`: searchable cluster/database chooser, first-run configuration guidance, settings, confirmation, CSV path and help dialogs. All focusable and keyboard reachable.
- `workspace.py`: one schema tree/editor/result/prompt-context state owner per tab. Closing a tab cancels its tasks before destroying database resources. Responses target the owning tab, never whichever happens to be active later.
- `app.py`: top-level tabs, status, navigation and panel actions; no database implementation.

## Contracts and safety
- Each Run is one SQL statement in one transaction: success commits, failure rolls back. Explicit transaction-control, SET/RESET and COPY commands are rejected; no persistent transaction/session promise across pooled connections. Read-only by default, deliberate write-mode opt-in. No automatic query retry, including uncertain commit outcomes.
- Result display is bounded to configured rows; libpq still receives the full result, so use SQL LIMIT for large queries. Truncation is explicit and CSV exports exactly the retained result with a truncation warning. File creation is exclusive and owner-only (0600), never silent overwrite. Grid cell previews are capped at 2,000 characters; CSV retains complete cell values.
- Two warm connections per open tab; connection maximum explicit. Health checks, checkout validation, socket keepalives and connect/statement timeouts; pool worker retries with backoff, maintenance restarts exhausted reconnect cycles. Status reports measured connection count/age/errors and application payload estimates, not fabricated wire-byte counters.
- Schema metadata is untrusted data in AI context. Default `gpt-5.6-terra`, medium reasoning, Fast mode (`service_tier=fast`); editable model string and reasoning selector. `store=false`; no query results included. Settings explain paid fast tier and metadata/SQL disclosure.
- `.env` config: comma-separated cluster identifiers select uppercase PGDESK_<ID>_* sections with unique labels, exactly one credential source, maintenance database, optional database allowlist and production marker. PGDESK_OPENAI_* stores the AI secret reference and dot-separated key path. No embedded secrets or implicit shell interpolation/overrides. Local settings retain theme, model, reasoning, fast and system_prompt.

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
From this repository: `uv run pgdesk`. Source/editable installs always load this repository's `.env`, regardless of launch directory; wheel installs use `~/.config/pgdesk/.env`. Edit that file for cluster names, AWS profiles, regions and secret names; `.env.example` documents every option. Or pass `--config /path/to/.env`. Values remain in Secrets Manager or a separately supplied process environment. AWS RDS connections verify TLS against the standard libpq CA trust location. F1 contains the complete keyboard and execution reference; F9 configures AI preferences.

## AWS credential configuration follow-up
User authorizes using the miscellaneous Slack application's OpenAI key and test/production RDS administrator secrets through named local AWS profiles. Discovery confirmed the Slack application subsection's nested OpenAI key and both database secrets' standard RDS JSON shape. Account-specific references live only in the ignored `.env`.

- Add a small `aws_secrets.py` adapter around the installed AWS CLI. Config stores only explicit profile/region/secret references and an OpenAI key path. Secret values remain in memory, never in config, diagnostics, arguments or repository artifacts.
- `Cluster.connection_kwargs()` resolves RDS JSON fields on physical connection creation so reconnects obtain current credentials. AWS-backed PostgreSQL uses verified TLS with libpq's existing trust configuration; service/DSN behavior remains unchanged.
- `SqlAssistant` resolves the configured key off the UI thread, serializes lazy client initialization, and retains the key only in the OpenAI client. Explicit AWS configuration takes precedence over ambient OPENAI_API_KEY; AWS failures never silently fall back to another credential.
- User clarification makes the repository's ignored `.env` the editable configuration source for `test` and `prod`; remove the obsolete TOML loader, example and task-created local TOML file. Keep the production marker and existing server-enforced read-only default. No database writes or infrastructure mutations are authorized.
- Verification: sanitized live secret shape inspection; actual test/prod database discovery, warm pools and read-only constant queries; direct OpenAI request with synthetic schema only; keyboard chooser smoke; isolated secret-error/config/initialization edge tests and existing regression suite. No production schema or row data is sent to OpenAI by the smoke.

### Follow-up verification results
- `uv run ruff check src tests`: passed; `uv build`: source distribution and wheel built. `PGDESK_TEST_DSN=<disposable loopback PostgreSQL 15 DSN> uv run pytest -q`: 25 passed, including secret-payload suppression, credential-source precedence, concurrent client cleanup and dotenv environment isolation.
- Final `.env` used for real database discovery: 36 test databases, 18 production databases. Both sessions warmed two connections and passed constant queries with `transaction_read_only=on` and verified TLS. No business rows read or database writes performed.
- Actual TUI keyboard smoke opened both configured `postgres` workspaces, preloaded catalogs, ran read-only constants, opened settings and closed both pools. Rendered screenshot visually inspected. Actual CLI launched without `--config`, loaded the repository `.env`, reached test/postgres READY and exited cleanly with Ctrl+Q (exit 0).
- Live OpenAI Responses request through `.env` and its configured secret succeeded with synthetic schema only, using `gpt-5.6-terra` and Fast mode; returned service tier was `priority`. No production schema or data sent to OpenAI.
- Old TOML configuration removed; `.env.example` documents the sole supported cluster configuration format. Existing tabs retain their connection reference until reopened; restart after changing OpenAI authentication references.

### Configuration portability correction
- Anchor the default dotenv path to the source repository or installed user's config directory; preserve the explicit `--config` override. Never select an unrelated project's dotenv merely because PGDesk was launched there.
- Keep the committed OpenAI example generic; retain actual references only in the ignored local `.env`.
- Verification: Ruff passed; source distribution and wheel built. Pytest: 17 passed, 10 database tests skipped because the disposable fixture had been removed after the prior 25-test full run; no database implementation changed.
- Actual editable CLI launched outside the repository, loaded the existing AWS configuration, reached test/postgres READY and exited 0. Installed wheel launched in an isolated home from a directory containing a conflicting dotenv: selected user config by default and the explicit file with `--config`; both exited 0. Initial smoke captured the chooser before its list rendered and attempted quit while the modal was open; corrected the observation wait and closed the modal before quitting.

## Recent-record browsing and direct SQL prompting

### Approved outcomes and current facts
- User requests light latest-100 and heavy latest-100 hotkeys; automatic light latest-5 on table selection; AI inference of useful narrow columns and recency ordering on first table view; duplicate tabs; a smaller input-only AI pane; SQL-only replies inserted into the editor for F5.
- Existing tree selection only drafts unordered `SELECT * LIMIT 100`; the catalog has column types/defaults/constraints but no index metadata. `DatabaseSession.execute` enforces a transaction per run and one active query per workspace. The existing default 120-second timeout is not an appropriate automatic-preview budget.
- Existing `PgDesk.open_workspace` deduplicates cluster/database pairs. Each workspace owns its pool, SQL, results and conversation. Preserve that resource isolation while allowing explicit duplicates.
- The AI interface already uses Responses, complete schema metadata and per-tab history. No new provider, persistent store, business schema, event or AWS resource is needed.
- Product design is omitted: user supplied observable outcomes. Program/system design is limited to the new automatic-read seam and its ownership. No specialist delegates are configured; implementation and verification remain local.

### Recommended interaction contract
- Table selection/Enter: light latest 5, automatically refreshed. Ctrl+L: light latest 100. Ctrl+G: heavy latest 100. Ctrl+D: duplicate current workspace with its selected table and SQL, independent pool, fresh results and read-only mode; no implicit execution of the copied SQL.
- F2 shows and focuses the AI prompt; F3/F4 similarly focus SQL/results. Pressing the same shortcut when its primary widget is already focused hides that panel. Keep Tab/Shift+Tab focus navigation.
- AI occupies a compact input/status strip, not a conversation transcript. Enter requests SQL; a validated SQL-only answer goes directly into the editor without running. Preserve in-memory context for follow-up prompts and include the current editor SQL. Remove obsolete Load SQL controls/actions and prose-bearing response state.
- Automatic replacements may overwrite the last program-generated query, not silently discard operator edits. If the editor changes during an AI request, require replacement confirmation. A late reply must neither overwrite a newer table selection nor steal focus from a different tab.
- First table use asks AI from metadata only; cache a validated browsing recipe for this application session, shared by duplicate tabs. Schema refresh invalidates the affected database's recipes. Never hold a database connection while awaiting OpenAI.

### Schema register and automatic-read contract
- Consumed: PostgreSQL catalog relation/column/constraint metadata; add index definitions and lightweight width metadata for column selection without reading value samples. Source PostgreSQL owns these facts. No database schema/index changes, migrations, durable cache or events.
- AI input: one relation's metadata and a constrained latest-row query request. AI output remains SQL only. Treat that SQL as an untrusted recipe: accept only a single-table SELECT over known columns, an explicit recency order and bounded limit; reject joins, subqueries, mutation, arbitrary functions and other clauses at the automatic seam.
- Compile the validated column/order choices with safely quoted identifiers. Light excludes large structured/binary payloads and caps textual previews; heavy uses `SELECT *` over the same ordering. NULL remains visible. The chosen ordering is visible in the SQL editor rather than an unexplained claim that the table has a universal insertion timestamp.
- Automatic browsing always uses server-enforced read-only transactions regardless of the tab's manual write toggle. Check the plan without executing it; enforce short statement/lock deadlines and never silently create indexes or retry SQL.
- Unresolved product choice: when no cheap latest-row plan exists, skipping automatic execution preserves latest-row semantics, while an explicitly labelled unordered sample preserves convenient inspection but is not evidence of freshness. A LIMIT alone does not avoid an unindexed full-table sort. Ask the user to choose this fallback before finalizing the automatic-read contract.

### Recommended program surfaces
1. `Relation.indexes: tuple[str, ...]` and per-column width metadata — owned by `catalog.py`, consumed by AI planning. Immutable catalog facts, not guessed access paths. Extend existing catalog loading/context instead of a second schema loader.
2. `BrowsePlan.from_sql(relation: Relation, statement: str) -> BrowsePlan` and `BrowsePlan.sql(limit: int, heavy: bool = False) -> str` — owned by new `browsing.py`, used by the planner and database preview. Validate one restricted recipe, then compile both projections and limits; keep arbitrary model SQL away from automatic execution.
3. `BrowsePlanner.plan(cluster: Cluster, database: str, relation: Relation, settings: Settings) -> BrowsePlan` (async), plus `invalidate` and `close` — app-owned session cache and shared in-flight requests. Workspace callers borrow plans, not database connections. Failed requests are visible and do not become successful cache entries; shutdown owns cancellation/awaiting.
4. `DatabaseSession.preview(plan: BrowsePlan, limit: int, heavy: bool = False) -> PreviewResult` — database-owned read-only execution, plan guard, short deadlines and the existing cancellation/row-retention path. Returns actual executed SQL, retained results and ordered/sample provenance. Manual `execute`/F5 retains its explicit operator contract. The accepted unordered fallback belongs here, not in widgets.
5. `SqlAssistant.suggest(...) -> Suggestion(sql: str, tier: str)` — AI-owned single-statement SQL response validation, shared by prompt and first-view recipe inference. No prose/transcript output contract. Preserve explicit credentials, schema-only disclosure and the existing SDK transport.
6. `PgDesk.duplicate_workspace(source: Workspace) -> Workspace` (async) — app-owned independent tab creation; normal chooser deduplication remains. Workspace retains selected relation, editor revision/selection generation and owned operation tasks; result buffers and active queries are never shared.

### Call paths, ordering and cutover
- Selection → workspace generation/draft guard → shared BrowsePlanner → existing SqlAssistant → restricted BrowsePlan → DatabaseSession.preview → originating workspace's SQL/results.
- Prompt → capture editor/table/context → SqlAssistant → single-statement validation → originating editor if unchanged, otherwise explicit replacement confirmation → F5 only.
- Duplicate → capture source selection/SQL/context → app mounts a separate pool-owning workspace → no query execution; subsequent edits and cancellations remain tab-local.
- Rapid selection cancels/awaits prior automatic reads and fences stale results. It must not cancel an unrelated manually submitted write. F6 and shutdown release query borrowers before closing pools; cancelling an asyncio waiter alone cannot stop its Psycopg thread.
- Atomic caller migration: catalog loader/context and tests; AI response consumers/tests; workspace selection/query/prompt controls; app bindings/tab creation; F1/settings/default prompt; existing plan/checklist. Remove `Relation.preview_sql`, obsolete Load AI SQL paths, RichLog/transcript UI and redundant suggestion prose, rather than retaining compatibility shims.

### Proof and next work
- Implement the settled interaction and browsing contracts, then exercise them with disposable synthetic PostgreSQL tables: indexed integer identity, UUID plus indexed creation time, timestamp ties/NULLs, absent ordering index, large payload columns, empty relations and quoted identifiers.
- Defend restricted-query rejection before database effects and compare latest-row results against an independent expected ordering. Cover failed AI planning, concurrent plan reuse, metadata invalidation and cancellation where materially uncertain.
- Actual TUI smoke: automatic 5, both 100-row hotkeys, AI text entry/SQL insertion, late reply versus newer selection/editor edits, duplicate SQL/pool independence, F2 focus, panel sizing, F6 and complete shutdown. Use synthetic schema/data only for live OpenAI verification.
- Run focused checks and the database suite, inspect the actual layout, update existing help/docs with observed evidence, review the complete diff and commit locally. Do not claim index-independent cheap latest-row retrieval or execute live business previews for verification.

### Accepted fallback
- User selected **Show an unordered sample** when no cheap latest-row plan is available. Compile the same light/heavy projection and requested row cap without ORDER BY, explicitly label it `UNORDERED SAMPLE — NOT LATEST`, and show the SQL actually executed.
- Check the sample plan too; views/foreign relations or expensive sample plans remain manual rather than defeating the promised lightweight behavior. Statement/lock deadlines bound server work, not model/network latency.
- If AI planning fails, report the failure and show a deterministic light unordered sample rather than claiming an inferred recency recipe. Do not cache the failure as a successful plan.

### Added appearance request and value-inspection advice
- User additionally requests a theme selector in Settings with every Oracle TUI theme. Copy the resolved palette catalog into this standalone package, not a runtime dependency on the private Oracle repository. Discovery found 16 available themes; all role colors and order were copied to `themes.json`.
- Persist the selected theme with existing settings, apply it to every open workspace on Save, and theme SQL syntax/cursors as well as shell backgrounds. Existing settings without a theme use Oracle's default `dark`.
- User asked for ideas, not implementation, for large JSON values. Recommended an Enter-on-cell inspector with full retained values, pretty JSON, search, scrolling, collapsible structure and copy/export. No inspector is included without a further request. Heavy query results already retain full decoded values; light projections intentionally avoid fetching large payloads.

### Observed verification
- Disposable local PostgreSQL, no business records: `uv run pytest -q` with the synthetic test DSN — **37 passed**. Includes indexed timestamp ordering and UUID tie-breakers, NULLs-last ordering, narrow text versus complete heavy JSON, an expensive unindexed sort becoming an explicitly unordered sample, and a lock deadline releasing its borrower.
- SQL authority probes reject joins, arbitrary functions, subquery limits, wrong relations and mutation/multi-statement recipes. Async SDK-transport regression verifies overlapping plan borrowers, cancellation isolation, cached reuse, refresh invalidation and pending-request shutdown.
- Actual Textual application with Pilot keyboard interaction and a synthetic OpenAI transport: automatic 5; both 100-row hotkeys; full retained heavy values; one inference per table; F2/Tab text focus; direct SQL-only insertion without execution; independent duplicate editors/pools/results; late inference fenced after newer table selection; explicit NOT LATEST sample; clean pool/task shutdown — passed.
- All 16 copied Oracle palettes applied to the shell and SQL editor; F9 keyboard selection saved and reloaded — passed. Dark/light screenshots visually inspected; native truecolor screenshot confirmed Oracle shell and SQL syntax colors. The smoke uses an isolated HOME/settings file.
- Live OpenAI Responses using configured credentials and **synthetic schema only**: inferred usable descending creation-time plans for integer-key and UUID-key tables; each ran a 5-row light preview; a separate prompt returned validated SQL only. Observed service tier `priority`. No query results were sent to the model.
- UI smoke exposed and fixed tree-cursor restoration before layout and chooser reload dropping an explicitly supplied settings path. Preview completion checks query-task ownership before re-enabling Run.
- Extended UI smoke passed failed-inference fallback/retry, empty-table results, F6 stopping an active server query and re-enabling Run, browsing not cancelling a manual run, and a delayed AI reply preserving newer editor text after replacement was declined. The F6 probe temporarily replaced the SQL compiler with a controlled sleep query against disposable data; production recipe validation stayed unchanged.
- The actual `.venv/bin/pgdesk` executable launched from `/tmp` in a PTY; F1 displayed the new shortcuts, Escape dismissed help and Ctrl+Q exited cleanly. Updated help was visually inspected.
- Ruff check/format passed for `src` and `tests`; `uv build` produced wheel and source distribution. Wheel inspection found the browsing module and all 16 standalone palettes, with no dotenv or smoke artifacts. Reviewed the integration diff; removed throwaway fixtures and committed the feature locally.

## Retained-cell inspector
- Follow-up request authorizes the proposed inspector and single-click activation. `ResultTable` preserves native header/scroll behavior but selects data on the first click; Enter follows the same `CellSelected` path. `Workspace.inspect_cell` captures the retained value, not the 2,000-character grid preview.
- `ValueInspector` owns pretty/raw text, lazy collapsible JSON branches, exact case-sensitive search with wraparound, terminal clipboard requests and explicit owner-only UTF-8 exports. Tree labels abbreviate long leaves; complete text remains available in pretty/raw views. No inspector action queries PostgreSQL or OpenAI.
- Result-scoped light provenance warns about capped text and NULL-indicator placeholders. The operator closes detail and requests Ctrl+G before inspecting the full source value; no hidden cell re-fetch or guessed primary-key lookup.
- Verification: **39 tests passed** against disposable PostgreSQL 16, including JSON value preservation and no-overwrite/0600 export contracts. Actual application/Pilot smoke passed first-click/Enter activation, 700-line JSON, long text, Unicode search/wraparound, raw/pretty/tree switching, collapse/re-expand, copy, export collision handling, NULL/binary rendering, header-click exclusion and light-to-heavy provenance. Screenshots visually inspected.
- Real PTY smoke opened detail with Enter, emitted OSC52 on Ctrl+Y, closed with Escape and exited cleanly. This proves clipboard transport, not that a particular terminal/OS clipboard accepted it; the UI states that dependency. No production data or live AI was used.
- Updated F1 help; Ruff and wheel/source builds passed. Temporary smoke artifacts and the owned PostgreSQL container are removed after verification.

### Large-value highlighting experiment
- Target: reduce synchronous text-widget construction by at least 25% for large retained JSON without changing any value, search or export output. Baseline revision `46acdac`; local Apple Silicon, Python 3.13 environment; no network/database; one process, four serial constructions per mode with GC between samples, first sample excluded.
- Synthetic workload: 20,000 objects, 1,828,892 pretty-printed characters / 80,002 lines. `cProfile` attributed substantial CPU to synchronous highlight-map construction and language/document initialization; ordinary text layout also contributes.
- Same-workload paired remeasurement: highlighting enabled median **1.276s** (three warm samples, range 1.248–1.282s); disabled median **0.518s** (range 0.510–0.526s), approximately 59% lower. This is a widget-construction microbenchmark, not a production or end-to-end latency claim.
- Keep: JSON syntax highlighting only for initial pretty representations up to 200,000 characters. Larger values remain fully available as plain pretty/raw text; switching representation does not re-enable the parser. No data truncation or deferred background parsing.
- Actual inspector smoke on that large payload verified complete pretty/raw values, a late search match, copy/export equality and successful close; a small JSON value retained highlighting. Focused preservation/export regressions passed. No flaky wall-clock regression test was added.

### Wide-tree expansion bound
- Baseline `74dc980`, same local Python/Textual environment: expanding a synthetic 80,000-element array allocated 80,000 nodes synchronously. Four serial samples with GC between runs, first excluded: median **0.515s**, range 0.507–0.524s.
- Cap each expansion at 1,000 data children plus a counted overflow notice directing the operator to Pretty/Raw or search. Identical post-change experiment: 1,001 nodes, median **5.59ms**, range 5.57–5.68ms. Keep this bound; it changes tree presentation, not retained content, and avoids the measured allocation burst without deferred work.
- Actual inspector smoke verified the visible 79,000-entry overflow notice, stable collapse/re-expansion, search finding element 79,999 and full pretty-text/clipboard equality. Screenshot inspected. Array/object allocation-budget regressions plus existing preservation/export tests: **4 passed**. No timing assertions in the permanent suite.
