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
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static, TextArea, Tree

from pgdesk.ai import SqlAssistant, Suggestion
from pgdesk.catalog import Catalog, QueryResult, Relation, cell_text
from pgdesk.config import Cluster, Config
from pgdesk.database import DatabaseSession, error_text
from pgdesk.screens import ConfirmScreen, ExportScreen


class Workspace(Horizontal):
    """Own every mutable tab-local object and fence late asynchronous results on close."""

    DEFAULT_CSS = """
    Workspace { height: 1fr; }
    Workspace .sidebar { width: 30; min-width: 18; border: round $primary; }
    Workspace .sidebar Input { height: 3; }
    Workspace Tree { height: 1fr; scrollbar-size: 1 1; }
    Workspace .panels { width: 1fr; }
    Workspace .work-panel { height: 1fr; min-height: 5; border: round $primary; }
    Workspace .panel-title { height: 1; color: $accent; text-style: bold; padding: 0 1; }
    Workspace .panel-tools { height: 3; }
    Workspace .panel-tools Button { min-width: 9; height: 3; }
    Workspace .panel-tools Input { width: 1fr; }
    Workspace TextArea { height: 1fr; }
    Workspace RichLog { height: 1fr; padding: 0 1; }
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
        self.suggestion: Suggestion | None = None
        self.history: list[dict[str, str]] = []
        self.read_only = True
        self.closing = False
        self.query_task: asyncio.Task | None = None
        self.ai_task: asyncio.Task | None = None
        self.catalog_task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()
        self._shutdown_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        """Compose the left hierarchy and three equally weighted right-hand panels."""
        with Vertical(classes="sidebar"):
            yield Label(" SCHEMAS / RELATIONS", classes="panel-title")
            yield Input(placeholder="/ Filter schema or relation", id="tree-filter")
            yield Tree("Loading schema…", id="schema-tree")
        with Vertical(classes="panels"):
            with Vertical(id="ai-panel", classes="work-panel"):
                yield Label(" AI · schema-aware SQL drafts · F2", classes="panel-title")
                yield RichLog(
                    id="ai-log", wrap=True, markup=False, highlight=False, auto_scroll=True
                )
                with Horizontal(classes="panel-tools"):
                    yield Input(
                        placeholder="Ask for SQL · Enter sends · Ctrl+L loads draft", id="ai-input"
                    )
                    yield Button("Send", id="send-ai", variant="primary")
                    yield Button("Load SQL", id="load-ai")
            with Vertical(id="sql-panel", classes="work-panel"):
                yield Label(" SQL · READ ONLY · F3", id="sql-title", classes="panel-title")
                yield TextArea.code_editor(
                    "",
                    language="sql",
                    theme="monokai",
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
                    " RESULTS · arrows navigate · Ctrl+S exports · F4", classes="panel-title"
                )
                yield Static(
                    "Select a table to draft a query, or write SQL and press F5.",
                    id="result-status",
                    classes="result-status",
                    markup=False,
                )
                yield DataTable(id="results", zebra_stripes=True, cursor_type="cell")
            yield Static("F2 AI · F3 SQL · F4 Results", classes="empty-panels", id="empty-panels")

    def on_mount(self) -> None:
        """Begin schema preload and retry only missing metadata after connection recovery."""
        self.query_one("#empty-panels").display = False
        self.query_one("#ai-log", RichLog).write(
            "Ask for SQL using this tab's complete accessible schema. Drafts never execute automatically. F9 configures OpenAI."
        )
        self.refresh_catalog()
        self.set_interval(5, self.ensure_catalog)

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
            self.query_one("#sql-editor", TextArea).text.strip()
            or self.history
            or self.query_one("#ai-input", Input).value.strip()
        )

    def ensure_catalog(self) -> None:
        """Retry schema preload after startup failure without polling successful metadata."""
        if self.catalog_snapshot is None:
            self.refresh_catalog()

    def refresh_catalog(self) -> None:
        """Schedule one metadata refresh without replacing a usable prior snapshot on failure."""
        if self.closing or (self.catalog_task and not self.catalog_task.done()):
            return
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
                    for column in relation.columns:
                        relation_node.add_leaf(
                            Text(
                                f"{column.name} : {column.data_type}"
                                + (" NOT NULL" if column.not_null else "")
                            )
                        )
        tree.root.expand()

    @on(Tree.NodeSelected, "#schema-tree")
    def relation_selected(self, event: Tree.NodeSelected) -> None:
        """Draft a safely quoted preview; never run on tree navigation."""
        if isinstance(event.node.data, Relation):
            self.load_sql(event.node.data.preview_sql())

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

    def _replace_sql(self, statement: str) -> None:
        """Show and focus the editor with a chosen SQL draft."""
        self.query_one("#sql-panel").display = True
        self.update_empty_panels()
        editor = self.query_one("#sql-editor", TextArea)
        editor.load_text(statement)
        editor.focus()

    def toggle_panel(self, name: str) -> None:
        """Let Textual distribute equal fractional heights among only the visible panels."""
        panel = self.query_one(f"#{name}-panel")
        panel.display = not panel.display
        self.update_empty_panels()
        if not panel.display:
            self.query_one("#schema-tree", Tree).focus()

    def update_empty_panels(self) -> None:
        """Keep an actionable empty state when all three main panels are hidden."""
        self.query_one("#empty-panels").display = not any(
            self.query_one(f"#{name}-panel").display for name in ("ai", "sql", "data")
        )

    def set_status(self, text: str) -> None:
        """Display a plain-text result or error message without terminal markup injection."""
        self.query_one("#result-status", Static).update(text)

    def run_query(self) -> None:
        """Capture query and mode before scheduling work so later edits cannot retarget it."""
        if self.closing or (self.query_task and not self.query_task.done()):
            return
        editor = self.query_one("#sql-editor", TextArea)
        statement = editor.selected_text or editor.text
        if not statement.strip():
            self.set_status("Write SQL or select a relation first.")
            return
        self.query_one("#data-panel").display = True
        self.update_empty_panels()
        self.query_task = self.spawn(self._execute(statement, self.read_only))

    async def _execute(self, statement: str, read_only: bool) -> None:
        """Replace stale output immediately and publish committed results or a visible error."""
        self.result = None
        table = self.query_one("#results", DataTable)
        table.clear(columns=True)
        self.query_one("#run", Button).disabled = True
        self.set_status(
            "Running READ ONLY… F6 cancels"
            if read_only
            else "Running WRITE transaction… F6 cancels; success commits"
        )
        try:
            result = await asyncio.to_thread(self.session.execute, statement, read_only)
            if self.closing:
                return
            self.result = result
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
                if self.closing:
                    return
            suffix = (
                f" · TRUNCATED to {len(result.rows):,} retained rows (CSV exports these only)"
                if result.truncated
                else ""
            )
            self.set_status(
                f"{result.status} · {result.elapsed:.3f}s · {len(result.rows):,} rows{suffix}"
            )
        except Exception as error:
            if not self.closing:
                self.set_status(error_text(error))
        finally:
            if not self.closing:
                self.query_one("#run", Button).disabled = False

    async def cancel_query(self) -> None:
        """Request database cancellation off the UI thread and report cancellation failures."""
        if self.query_task and not self.query_task.done():
            try:
                await asyncio.to_thread(self.session.cancel)
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
                + "? Every successful Run commits. AI never runs SQL."
            ),
            lambda yes: self._set_read_only(False) if yes and not self.closing else None,
        )

    def _set_read_only(self, value: bool) -> None:
        """Update the local execution mode and prominent editor label together."""
        self.read_only = value
        self.query_one("#sql-title", Label).update(
            " SQL · "
            + ("READ ONLY" if value else "WRITES ENABLED · AUTO-COMMIT ON SUCCESS")
            + " · F3"
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
            self.query_one("#ai-log", RichLog).write(
                "Schema is not loaded yet. Wait for connection recovery or press F7."
            )
            return
        self.ai_task = self.spawn(self._ask_ai(prompt))

    async def _ask_ai(self, prompt: str) -> None:
        """Keep replies in their source tab even if the operator switches workspaces."""
        log = self.query_one("#ai-log", RichLog)
        self.query_one("#send-ai", Button).disabled = True
        self.query_one("#ai-input", Input).disabled = True
        log.write(Text("You: " + prompt, style="bold"))
        log.write("Requesting SQL…")
        try:
            suggestion = await self.assistant.suggest(
                self.app.settings, self.catalog_snapshot, self.history, prompt
            )
            if self.closing:
                return
            self.suggestion = suggestion
            self.history.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": suggestion.text},
                ]
            )
            self.query_one("#ai-input", Input).value = ""
            log.write(Text(suggestion.text))
            log.write(f"Service tier: {suggestion.tier} · Ctrl+L loads SQL without executing")
        except APIStatusError as error:
            log.write(
                f"OpenAI HTTP {error.status_code}. Check model access, Fast tier, reasoning and API key (F9). No fallback model was used."
            )
        except OpenAIError:
            log.write(
                "OpenAI unavailable. Check the configured AWS secret or OPENAI_API_KEY and connectivity/settings (F9). Request was not retried."
            )
        except Exception as error:
            log.write(error_text(error))
        finally:
            if not self.closing:
                self.query_one("#send-ai", Button).disabled = False
                self.query_one("#ai-input", Input).disabled = False

    def load_ai(self) -> None:
        """Insert only a concrete latest SQL draft, never execute it or guess prose is SQL."""
        if self.suggestion and self.suggestion.sql:
            self.load_sql(self.suggestion.sql)
        else:
            self.query_one("#ai-log", RichLog).write(
                "No single SQL draft available; ask for one statement in a SQL code fence."
            )

    def clear_ai(self) -> None:
        """Clear only this tab's conversation when no request is using its history."""
        if self.ai_task and not self.ai_task.done():
            return
        self.history.clear()
        self.suggestion = None
        self.query_one("#ai-log", RichLog).clear()

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
            case "load-ai":
                self.load_ai()

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
        try:
            await asyncio.to_thread(self.session.cancel)
        finally:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            await asyncio.to_thread(self.session.close)
