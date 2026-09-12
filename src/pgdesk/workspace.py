"""One database workspace: tree, draft editor, retained result and private AI conversation."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from openai import APIStatusError, OpenAIError
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, Static, TextArea, Tree

from pgdesk.ai import SqlAssistant
from pgdesk.browsing import BrowsePlan
from pgdesk.catalog import Catalog, QueryResult, Relation, cell_text
from pgdesk.config import Cluster, Config
from pgdesk.database import DatabaseSession, error_text
from pgdesk.screens import ConfirmScreen, ExportScreen
from pgdesk.themes import editor_theme


class Workspace(Horizontal):
    """Own every mutable tab-local object and fence late asynchronous results on close."""

    DEFAULT_CSS = """
    Workspace { height: 1fr; }
    Workspace .sidebar { width: 30; min-width: 18; border: round $primary; }
    Workspace .sidebar Input { height: 3; }
    Workspace Tree { height: 1fr; scrollbar-size: 1 1; }
    Workspace .panels { width: 1fr; }
    Workspace .work-panel { height: 1fr; min-height: 5; border: round $primary; }
    Workspace #ai-panel { height: 8; min-height: 8; }
    Workspace #ai-status { height: 2; padding: 0 1; color: $text-muted; }
    Workspace .panel-title { height: 1; color: $accent; text-style: bold; padding: 0 1; }
    Workspace .panel-tools { height: 3; }
    Workspace .panel-tools Button { min-width: 9; height: 3; }
    Workspace .panel-tools Input { width: 1fr; }
    Workspace TextArea { height: 1fr; }
    Workspace DataTable { height: 1fr; }
    Workspace .result-status { height: auto; max-height: 5; padding: 0 1; }
    Workspace .empty-panels { height: 1fr; content-align: center middle; color: $text-muted; }
    """

    def __init__(
        self, cluster: Cluster, database: str, config: Config, assistant: SqlAssistant, **kwargs
    ) -> None:
        """Create a pool and independent widget state for exactly one cluster/database pair."""
        super().__init__(**kwargs)
        self.cluster = cluster
        self.database = database
        self.config = config
        self.assistant = assistant
        self.session = DatabaseSession(cluster, database, config)
        self.catalog_snapshot: Catalog | None = None
        self.result: QueryResult | None = None
        self.selected_relation: Relation | None = None
        self._generated_sql = ""
        self._selection_generation = 0
        self._browse_task: asyncio.Task | None = None
        self._query_is_preview = False
        self.history: list[dict[str, str]] = []
        self.read_only = True
        self.closing = False
        self.query_task: asyncio.Task | None = None
        self.ai_task: asyncio.Task | None = None
        self.catalog_task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()
        self._shutdown_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        """Compose schema navigation, a compact AI prompt, and equally weighted SQL/results."""
        with Vertical(classes="sidebar"):
            yield Label(" SCHEMAS / RELATIONS", classes="panel-title")
            yield Input(placeholder="/ Filter schema or relation", id="tree-filter")
            yield Tree("Loading schema…", id="schema-tree")
        with Vertical(classes="panels"):
            with Vertical(id="ai-panel", classes="work-panel"):
                yield Label(" AI PROMPT · F2 focuses · Enter drafts SQL", classes="panel-title")
                with Horizontal(classes="panel-tools"):
                    yield Input(
                        placeholder="Ask for SQL… Enter puts the answer in the editor",
                        id="ai-input",
                    )
                    yield Button("Send", id="send-ai", variant="primary")
                yield Static(
                    "SQL only · F5 executes · Ctrl+K clears prompt context",
                    id="ai-status",
                    markup=False,
                )
            with Vertical(id="sql-panel", classes="work-panel"):
                yield Label(" SQL · READ ONLY · F3 focuses", id="sql-title", classes="panel-title")
                yield TextArea.code_editor(
                    "",
                    language="sql",
                    theme="css",
                    id="sql-editor",
                    tab_behavior="focus",
                    soft_wrap=False,
                )
                with Horizontal(classes="panel-tools"):
                    yield Button("Run F5", id="run", variant="primary")
                    yield Button("Cancel F6", id="cancel-query")
                    yield Static(
                        "One statement · commits on success · selection or whole editor",
                        classes="result-status",
                    )
            with Vertical(id="data-panel", classes="work-panel"):
                yield Label(
                    " RESULTS · Ctrl+L light 100 · Ctrl+G heavy 100 · F4 focuses",
                    classes="panel-title",
                )
                yield Static(
                    "Select a table for a light 5-row preview, or write SQL and press F5.",
                    id="result-status",
                    classes="result-status",
                    markup=False,
                )
                yield DataTable(id="results", zebra_stripes=True, cursor_type="cell")
            yield Static("F2 AI · F3 SQL · F4 Results", classes="empty-panels", id="empty-panels")

    def on_mount(self) -> None:
        """Begin schema preload and retry only missing metadata after connection recovery."""
        self.query_one("#empty-panels").display = False
        self.apply_theme()
        self.set_ai_status("SQL only · F5 executes · Ctrl+K clears prompt context")
        self.refresh_catalog()
        self.set_interval(5, self.ensure_catalog)

    def apply_theme(self) -> None:
        """Apply the saved Oracle palette to SQL syntax as well as the surrounding interface."""
        editor = self.query_one("#sql-editor", TextArea)
        theme = editor_theme(self.app.settings.theme)
        editor.register_theme(theme)
        editor.theme = theme.name

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """Retain a tab task until completion so close can await all resource borrowers."""
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @property
    def has_draft(self) -> bool:
        """Report in-memory work that would be lost by closing this tab."""
        return bool(
            (
                self.query_one("#sql-editor", TextArea).text.strip()
                and self.query_one("#sql-editor", TextArea).text != self._generated_sql
            )
            or self.history
            or self.query_one("#ai-input", Input).value.strip()
        )

    def ensure_catalog(self) -> None:
        """Retry schema preload after startup failure without polling successful metadata."""
        if self.catalog_snapshot is None:
            self.refresh_catalog()

    def refresh_catalog(self, *, replan: bool = False) -> None:
        """Schedule one metadata refresh without replacing a usable prior snapshot on failure."""
        if self.closing or (self.catalog_task and not self.catalog_task.done()):
            return
        if replan:
            self.app.browsing.invalidate(self.cluster, self.database)
            self._selection_generation += 1
            if self._browse_task and not self._browse_task.done():
                self._browse_task.cancel()
        self.catalog_task = self.spawn(self._load_catalog())

    async def _load_catalog(self) -> None:
        """Publish metadata only to its originating tab; leave failures visible and retryable."""
        try:
            catalog = await asyncio.to_thread(self.session.catalog)
        except Exception as error:
            if not self.closing:
                self.query_one("#schema-tree", Tree).root.set_label(
                    Text("Schema unavailable · F7 retries")
                )
                self.set_status(error_text(error))
            return
        if not self.closing:
            self.catalog_snapshot = catalog
            if self.selected_relation is not None:
                key = (self.selected_relation.schema, self.selected_relation.name)
                self.selected_relation = next(
                    (
                        relation
                        for relation in catalog.relations
                        if (relation.schema, relation.name) == key
                    ),
                    None,
                )
            self.rebuild_tree()

    @on(Input.Changed, "#tree-filter")
    def filter_tree(self) -> None:
        """Filter schema and relation names without changing the schema sent to AI."""
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        """Build schema → tables/views → relation → columns with safely rendered labels."""
        if self.catalog_snapshot is None:
            return
        tree = self.query_one("#schema-tree", Tree)
        tree.clear()
        tree.root.set_label(Text(self.database))
        query = self.query_one("#tree-filter", Input).value.casefold()
        selected_node = None
        for schema in self.catalog_snapshot.schemas:
            relations = [
                r
                for r in self.catalog_snapshot.relations
                if r.schema == schema and (query in r.name.casefold() or query in schema.casefold())
            ]
            if query and not relations and query not in schema.casefold():
                continue
            node = tree.root.add(Text(schema), expand=True)
            for name, is_view in (("Tables", False), ("Views", True)):
                group = node.add(name, expand=bool(query))
                for relation in relations:
                    if relation.is_view != is_view:
                        continue
                    relation_node = group.add(Text(relation.name), data=relation, expand=False)
                    if self.selected_relation and (relation.schema, relation.name) == (
                        self.selected_relation.schema,
                        self.selected_relation.name,
                    ):
                        selected_node = relation_node
                        group.expand()
                    for column in relation.columns:
                        relation_node.add_leaf(
                            Text(
                                f"{column.name} : {column.data_type}"
                                + (" NOT NULL" if column.not_null else "")
                            )
                        )
        tree.root.expand()
        if selected_node is not None:
            tree.call_after_refresh(tree.move_cursor, selected_node)

    @on(Tree.NodeSelected, "#schema-tree")
    def relation_selected(self, event: Tree.NodeSelected) -> None:
        """Refresh a light five-row preview when a relation is deliberately selected."""
        if isinstance(event.node.data, Relation):
            self.browse(relation=event.node.data)

    def load_sql(self, statement: str) -> None:
        """Confirm before overwriting another draft and preserve explicit execution."""
        editor = self.query_one("#sql-editor", TextArea)
        if editor.text.strip() and editor.text != statement:
            self.app.push_screen(
                ConfirmScreen(
                    "Replace the SQL editor draft in "
                    + self.cluster.name
                    + "/"
                    + self.database
                    + "?"
                ),
                lambda yes: self._replace_sql(statement) if yes and not self.closing else None,
            )
        else:
            self._replace_sql(statement)

    def _replace_sql(self, statement: str, *, preview: bool = False, focus: bool = True) -> None:
        """Publish SQL to this editor without stealing focus from a different workspace."""
        self.query_one("#sql-panel").display = True
        self.update_empty_panels()
        editor = self.query_one("#sql-editor", TextArea)
        editor.load_text(statement)
        self._generated_sql = statement if preview else ""
        if focus and self.app.active_workspace is self:
            editor.focus()

    def toggle_panel(self, name: str) -> None:
        """Show/focus a primary control, hiding only when that control already has focus."""
        panel = self.query_one(f"#{name}-panel")
        primary = self.query_one(
            {"ai": "#ai-input", "sql": "#sql-editor", "data": "#results"}[name]
        )
        if panel.display and primary.has_focus:
            panel.display = False
            self.query_one(".sidebar").display = True
            self.query_one("#schema-tree", Tree).focus()
        else:
            panel.display = True
            primary.focus()
        self.update_empty_panels()

    def update_empty_panels(self) -> None:
        """Keep an actionable empty state when all three main panels are hidden."""
        self.query_one("#empty-panels").display = not any(
            self.query_one(f"#{name}-panel").display for name in ("ai", "sql", "data")
        )

    def set_status(self, text: str) -> None:
        """Display a plain-text result or error message without terminal markup injection."""
        self.query_one("#result-status", Static).update(text)

    def set_ai_status(self, text: str) -> None:
        """Keep request progress/errors visible without an AI transcript pane."""
        self.query_one("#ai-status", Static).update(text)

    def copy_from(self, source: Workspace) -> None:
        """Copy drafts and immutable metadata, never results, running tasks or write permission."""
        self.catalog_snapshot = source.catalog_snapshot
        self.selected_relation = source.selected_relation
        self.history = [turn.copy() for turn in source.history]
        self.query_one("#ai-input", Input).value = source.query_one("#ai-input", Input).value
        self._replace_sql(source.query_one("#sql-editor", TextArea).text, focus=False)
        self._generated_sql = source._generated_sql
        self.rebuild_tree()

    def browse(
        self, limit: int = 5, heavy: bool = False, *, relation: Relation | None = None
    ) -> None:
        """Request a bounded table view without silently discarding manual work or cancelling writes."""
        target = relation or self.selected_relation
        if target is None:
            self.set_status("Select a table first; Ctrl+L light 100 · Ctrl+G heavy 100.")
            return
        if not target.columns:
            self.set_status("No visible columns for a light preview; use an explicit F5 query.")
            return
        if self.query_task and not self.query_task.done() and not self._query_is_preview:
            self.set_status(
                "A manual query is running. F6 cancels; table browsing did not interrupt it."
            )
            return
        editor = self.query_one("#sql-editor", TextArea)
        if editor.text.strip() and editor.text != self._generated_sql:
            self.app.push_screen(
                ConfirmScreen(
                    "Replace the edited SQL with a table preview? Duplicate with Ctrl+D to keep both."
                ),
                lambda yes: (
                    self._start_browse(target, limit, heavy) if yes and not self.closing else None
                ),
            )
            return
        self._start_browse(target, limit, heavy)

    def _start_browse(self, relation: Relation, limit: int, heavy: bool) -> None:
        """Fence older selections and own only the new metadata/AI preparation waiter."""
        if self.closing:
            return
        self._selection_generation += 1
        self.selected_relation = relation
        if self._browse_task and not self._browse_task.done():
            self._browse_task.cancel()
        before = self.query_one("#sql-editor", TextArea).text
        self.query_one("#data-panel").display = True
        self.update_empty_panels()
        self.set_status(
            f"Preparing {'heavy' if heavy else 'light'} {limit} · {relation.qualified}…"
        )
        self._browse_task = self.spawn(
            self._prepare_preview(relation, limit, heavy, self._selection_generation, before)
        )

    async def _prepare_preview(
        self, relation: Relation, limit: int, heavy: bool, generation: int, before: str
    ) -> None:
        """Await inference without a pool borrower; cancel/await an older automatic read first."""
        try:
            if self.query_task and not self.query_task.done():
                await asyncio.to_thread(self.session.cancel)
                await asyncio.shield(self.query_task)
        except Exception as error:
            if not self.closing and generation == self._selection_generation:
                self.set_status(error_text(error))
            return
        warning = ""
        try:
            plan = await self.app.browsing.plan(
                self.cluster, self.database, relation, self.app.settings
            )
        except asyncio.CancelledError:
            if not self.closing and generation == self._selection_generation:
                self.set_status("Preview preparation cancelled or invalidated; reselect to retry.")
            raise
        except Exception as error:
            warning = "AI recipe unavailable; showing a sample. " + error_text(error)
            plan = BrowsePlan.sample(relation)
        if self.closing or generation != self._selection_generation:
            return
        if self.query_one("#sql-editor", TextArea).text != before:
            self.set_status(
                "Preview not started: SQL changed while its recipe was being prepared. Reselect to retry."
            )
            return
        if warning:
            self.set_ai_status(warning)
        self._replace_sql(plan.sql(limit, heavy), preview=True, focus=False)
        self._query_is_preview = True
        self.query_task = self.spawn(self._execute_preview(plan, limit, heavy, generation))

    async def _execute_preview(
        self, plan: BrowsePlan, limit: int, heavy: bool, generation: int
    ) -> None:
        """Run an automatic read and publish only if its selection still owns the workspace."""
        self._begin_result("Checking preview cost, then reading… F6 cancels")
        before = self.query_one("#sql-editor", TextArea).text
        try:
            preview = await asyncio.to_thread(self.session.preview, plan, limit, heavy)
            if self.closing or generation != self._selection_generation:
                return
            if self.query_one("#sql-editor", TextArea).text == before:
                self._replace_sql(preview.statement, preview=True, focus=False)
            provenance = (
                "LATEST · AI-inferred order" if preview.ordered else "UNORDERED SAMPLE — NOT LATEST"
            )
            projection = (
                "HEAVY · all columns"
                if heavy
                else "LIGHT · text capped at 160; large fields show __is_null"
            )
            await self._publish_result(
                preview.result, f"{provenance} · {projection} · ", generation
            )
        except Exception as error:
            if not self.closing and generation == self._selection_generation:
                self.set_status(error_text(error))
        finally:
            if not self.closing and self.query_task is asyncio.current_task():
                self.query_one("#run", Button).disabled = False

    def run_query(self) -> None:
        """Capture query and mode before scheduling work so later edits cannot retarget it."""
        if self.closing or (self.query_task and not self.query_task.done()):
            return
        editor = self.query_one("#sql-editor", TextArea)
        statement = editor.selected_text or editor.text
        if not statement.strip():
            self.set_status("Write SQL or select a relation first.")
            return
        self._selection_generation += 1
        if self._browse_task and not self._browse_task.done():
            self._browse_task.cancel()
        self._query_is_preview = False
        self.query_one("#data-panel").display = True
        self.update_empty_panels()
        self.query_task = self.spawn(self._execute(statement, self.read_only))

    async def _execute(self, statement: str, read_only: bool) -> None:
        """Replace stale output immediately and publish committed results or a visible error."""
        self._begin_result(
            "Running READ ONLY… F6 cancels"
            if read_only
            else "Running WRITE transaction… F6 cancels; success commits"
        )
        try:
            result = await asyncio.to_thread(self.session.execute, statement, read_only)
            await self._publish_result(result)
        except Exception as error:
            if not self.closing:
                self.set_status(error_text(error))
        finally:
            if not self.closing:
                self.query_one("#run", Button).disabled = False

    def _begin_result(self, status: str) -> None:
        """Remove stale rows and disable Run while one operation owns the result surface."""
        self.result = None
        self.query_one("#results", DataTable).clear(columns=True)
        self.query_one("#run", Button).disabled = True
        self.set_status(status)

    async def _publish_result(
        self, result: QueryResult, prefix: str = "", generation: int | None = None
    ) -> None:
        """Render in bounded batches while fencing a superseded automatic selection."""
        if self.closing or (generation is not None and generation != self._selection_generation):
            return
        self.result = result
        table = self.query_one("#results", DataTable)
        for index, name in enumerate(result.columns):
            table.add_column(Text(name), key=str(index), width=min(60, max(12, len(name))))
        for offset in range(0, len(result.rows), 200):
            table.add_rows(
                [
                    tuple(Text(cell_text(value)[:2000]) for value in row)
                    for row in result.rows[offset : offset + 200]
                ]
            )
            await asyncio.sleep(0)
            if self.closing or (
                generation is not None and generation != self._selection_generation
            ):
                return
        suffix = (
            f" · TRUNCATED to {len(result.rows):,} retained rows (CSV exports these only)"
            if result.truncated
            else ""
        )
        self.set_status(
            f"{prefix}{result.status} · {result.elapsed:.3f}s · {len(result.rows):,} rows{suffix}"
        )

    async def cancel_query(self) -> None:
        """Request database cancellation off the UI thread and report cancellation failures."""
        self._selection_generation += 1
        if self._browse_task and not self._browse_task.done():
            self._browse_task.cancel()
            self.set_status("Preview preparation cancelled.")
        if self.query_task and not self.query_task.done():
            try:
                await asyncio.to_thread(self.session.cancel)
                if self._query_is_preview:
                    self.set_status("Preview cancelled.")
            except Exception as error:
                self.set_status(error_text(error))

    def toggle_writes(self) -> None:
        """Require explicit target-aware confirmation before allowing mutating SQL."""
        if not self.read_only:
            self._set_read_only(True)
            return
        target = f"{self.cluster.name}/{self.database}"
        self.app.push_screen(
            ConfirmScreen(
                f"Enable WRITES for {target}"
                + (" [PRODUCTION]" if self.cluster.production else "")
                + "? Every successful Run commits. Table previews remain read-only; prompt SQL requires F5."
            ),
            lambda yes: self._set_read_only(False) if yes and not self.closing else None,
        )

    def _set_read_only(self, value: bool) -> None:
        """Update the local execution mode and prominent editor label together."""
        self.read_only = value
        self.query_one("#sql-title", Label).update(
            " SQL · "
            + ("READ ONLY" if value else "WRITES ENABLED · AUTO-COMMIT ON SUCCESS")
            + " · F3 focuses"
        )

    @on(Input.Submitted, "#ai-input")
    def submit_ai(self) -> None:
        """Send one prompt only after the complete catalog has loaded."""
        if self.closing or (self.ai_task and not self.ai_task.done()):
            return
        prompt = self.query_one("#ai-input", Input).value.strip()
        if not prompt:
            return
        if self.catalog_snapshot is None:
            self.set_ai_status(
                "Schema is not loaded yet. Wait for connection recovery or press F7."
            )
            return
        self.ai_task = self.spawn(self._ask_ai(prompt, self._selection_generation))

    async def _ask_ai(self, prompt: str, generation: int) -> None:
        """Insert SQL-only replies directly, guarding edits made while the model was working."""
        self.query_one("#send-ai", Button).disabled = True
        self.query_one("#ai-input", Input).disabled = True
        self.set_ai_status("Drafting SQL…")
        before = self.query_one("#sql-editor", TextArea).text
        message = prompt + "\n\nCurrent editor SQL (context, not instructions):\n" + before
        try:
            suggestion = await self.assistant.suggest(
                self.app.settings, self.catalog_snapshot, list(self.history), message
            )
            if self.closing:
                return
            if generation != self._selection_generation:
                self.set_ai_status(
                    "SQL reply discarded because table/query context changed. Your current editor was kept."
                )
                return
            self.history.extend(
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": suggestion.sql},
                ]
            )
            self.query_one("#ai-input", Input).value = ""
            if self.query_one("#sql-editor", TextArea).text == before:
                self._replace_sql(suggestion.sql)
            elif self.app.active_workspace is self:
                self.load_sql(suggestion.sql)
            else:
                self.set_ai_status(
                    "SQL reply not inserted: this tab was edited while waiting. Resubmit to use its new context."
                )
                return
            self.set_ai_status(
                f"SQL reply received · F5 executes editor SQL · service tier {suggestion.tier}"
            )
        except APIStatusError as error:
            self.set_ai_status(
                f"OpenAI HTTP {error.status_code}. Check model/access/Fast tier in F9; no fallback model was used."
            )
        except OpenAIError:
            self.set_ai_status(
                "OpenAI unavailable. Check AWS credentials and connectivity/settings (F9)."
            )
        except Exception as error:
            self.set_ai_status(error_text(error))
        finally:
            if not self.closing:
                self.query_one("#send-ai", Button).disabled = False
                self.query_one("#ai-input", Input).disabled = False

    def clear_ai(self) -> None:
        """Clear only this tab's prompt context when no request is using it."""
        if self.ai_task and not self.ai_task.done():
            return
        self.history.clear()
        self.query_one("#ai-input", Input).value = ""
        self.set_ai_status("Prompt context cleared · Enter drafts SQL; F5 executes")

    def export(self) -> None:
        """Capture the current result so a later query cannot change the export target."""
        result = self.result
        if result is None or not result.columns:
            self.set_status("No result table to export.")
            return
        self.app.push_screen(
            ExportScreen(result.truncated),
            lambda path: (
                self.spawn(self._export(result, path)) if path and not self.closing else None
            ),
        )

    async def _export(self, result: QueryResult, path) -> None:
        """Write the chosen retained result off-thread, reporting exclusive-create failures."""
        try:
            count = await asyncio.to_thread(result.export_csv, path)
            if not self.closing:
                self.set_status(f"Exported {count:,} rows to {path}")
        except OSError as error:
            if not self.closing:
                self.set_status(
                    f"CSV export failed: {type(error).__name__}. Check directory/permissions; choose a new filename if it exists."
                )

    @on(Button.Pressed)
    async def button_pressed(self, event: Button.Pressed) -> None:
        """Expose hotkey operations as focusable controls without competing implementations."""
        event.stop()
        match event.button.id:
            case "run":
                self.run_query()
            case "cancel-query":
                await self.cancel_query()
            case "send-ai":
                self.submit_ai()

    async def shutdown(self) -> None:
        """Await the same shielded cleanup when tab close and application exit overlap."""
        if self._shutdown_task is None:
            self.closing = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        """Cancel AI/SQL, await every borrower, then dispose of the physical pool."""
        if self.ai_task and not self.ai_task.done():
            self.ai_task.cancel()
        if self._browse_task and not self._browse_task.done():
            self._browse_task.cancel()
        try:
            await asyncio.to_thread(self.session.cancel)
        finally:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            await asyncio.to_thread(self.session.close)
