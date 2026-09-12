"""Keyboard-first connection, preferences, confirmation and export dialogs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static, Switch, TextArea
from textual.widgets.option_list import Option

from pgdesk.config import Cluster, Config, Settings
from pgdesk.database import error_text, list_databases
from pgdesk.themes import THEME_NAMES


class Dialog(ModalScreen):
    """Common Escape cancellation and keyboard-accessible centered dialog styling."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel", priority=True)]
    DEFAULT_CSS = """
    Dialog { align: center middle; background: $background 70%; }
    Dialog > Vertical, Dialog > VerticalScroll { width: 80; max-width: 95%; height: auto; max-height: 92%; border: round $accent; background: $surface; padding: 1 2; }
    Dialog Label { margin-top: 1; }
    Dialog Input, Dialog Select { width: 1fr; }
    Dialog .buttons { height: 3; margin-top: 1; align-horizontal: right; }
    Dialog Button { margin-left: 1; }
    Dialog .hint { height: auto; color: $text-muted; }
    Dialog .error { height: auto; color: $error; }
    """


class ConnectScreen(Dialog):
    """Filter clusters then databases; asynchronous discovery never freezes the chooser."""

    DEFAULT_CSS = "ConnectScreen OptionList { height: 14; margin-top: 1; }"

    def __init__(self, config: Config, config_path: Path) -> None:
        """Keep chooser navigation local until a concrete pair is selected."""
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.cluster: Cluster | None = None
        self.databases: tuple[str, ...] = ()
        self.filtered_choices: list[Cluster | str] = []
        self.generation = 0

    def compose(self) -> ComposeResult:
        """Show filter, choices and an explicit database-name fallback."""
        with Vertical():
            yield Label("Open workspace · cluster / database", id="connect-title")
            yield Input(placeholder="Filter clusters…", id="choice-filter")
            yield OptionList(id="choices")
            yield Static("Enter selects · Up/Down navigate · Escape cancels", classes="hint")
            yield Static("", id="connect-error", classes="error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Open typed database", id="typed")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        """Populate cluster choices or explain first-run configuration."""
        self.rebuild()
        self.query_one("#choice-filter", Input).focus()
        if not self.config.clusters:
            self.query_one("#connect-error", Static).update(
                f"No clusters configured. Create {self.config_path} using .env.example, "
                "then press Ctrl+N again to reload. Use AWS secret, libpq service or DSN environment references."
            )

    def rebuild(self) -> None:
        """Filter current choices without interpolating names as terminal markup."""
        query = self.query_one("#choice-filter", Input).value.casefold()
        choices = self.config.clusters if self.cluster is None else self.databases
        self.filtered_choices = [
            item
            for item in choices
            if query in (item.name if isinstance(item, Cluster) else item).casefold()
        ]
        options = self.query_one("#choices", OptionList)
        options.clear_options()
        from rich.text import Text

        options.add_options(
            [
                Option(
                    Text(item.name + ("  [PROD]" if item.production else ""))
                    if isinstance(item, Cluster)
                    else Text(item)
                )
                for item in self.filtered_choices
            ]
        )
        if self.filtered_choices:
            options.highlighted = 0
        self.query_one("#typed", Button).disabled = self.cluster is None
        self.query_one("#back", Button).disabled = self.cluster is None

    @on(Input.Changed, "#choice-filter")
    def filter_changed(self) -> None:
        """Apply text filtering while retaining arrow-key navigation."""
        self.rebuild()

    async def on_key(self, event) -> None:
        """Route list navigation from the filter without stealing ordinary text."""
        if self.query_one("#choice-filter", Input).has_focus and event.key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            options = self.query_one("#choices", OptionList)
            if event.key == "down":
                options.action_cursor_down()
            else:
                options.action_cursor_up()

    @on(Input.Submitted, "#choice-filter")
    async def filter_submitted(self) -> None:
        """Open the highlighted choice or explicitly typed database when no match exists."""
        index = self.query_one("#choices", OptionList).highlighted
        if index is not None:
            await self.choose(index)
        elif self.cluster:
            self.open_typed()

    @on(OptionList.OptionSelected, "#choices")
    async def selected(self, event: OptionList.OptionSelected) -> None:
        """Advance into a cluster or return the selected database pair."""
        await self.choose(event.option_index)

    async def choose(self, index: int) -> None:
        """Start background discovery under a generation fence for Back/close races."""
        if not 0 <= index < len(self.filtered_choices):
            return
        item = self.filtered_choices[index]
        if self.cluster:
            self.dismiss((self.cluster, item))
            return
        self.cluster = item
        self.generation += 1
        generation = self.generation
        self.query_one("#choice-filter", Input).value = ""
        self.query_one(
            "#choice-filter", Input
        ).placeholder = "Filter databases, or type an exact name…"
        self.query_one("#connect-title", Label).update(f"Open workspace · {item.name} / database")
        self.query_one("#connect-error", Static).update("Discovering databases…")
        self.rebuild()
        self.run_worker(self.discover(item, generation), exclusive=True)

    async def discover(self, cluster: Cluster, generation: int) -> None:
        """Fetch connectable names without publishing results into a different chooser state."""
        try:
            databases = await asyncio.to_thread(list_databases, cluster)
        except Exception as error:
            if self.is_mounted and generation == self.generation:
                self.query_one("#connect-error", Static).update(
                    error_text(error) + " · You may type an exact database name."
                )
            return
        if self.is_mounted and generation == self.generation:
            self.databases = databases
            self.query_one("#connect-error", Static).update(f"{len(databases)} databases")
            self.rebuild()

    def open_typed(self) -> None:
        """Allow connecting when maintenance-database discovery is not permitted."""
        name = self.query_one("#choice-filter", Input).value.strip()
        if self.cluster and name:
            self.dismiss((self.cluster, name))

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        """Handle explicit Back, typed-name selection and cancellation."""
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "typed":
            self.open_typed()
        elif event.button.id == "back":
            self.generation += 1
            self.cluster = None
            self.databases = ()
            self.query_one("#choice-filter", Input).value = ""
            self.query_one("#choice-filter", Input).placeholder = "Filter clusters…"
            self.query_one("#connect-title", Label).update("Open workspace · cluster / database")
            self.query_one("#connect-error", Static).update("")
            self.rebuild()


class SettingsScreen(Dialog):
    """Edit the Oracle palette, model ID, reasoning, Fast mode and custom system prompt."""

    BINDINGS: ClassVar = [*Dialog.BINDINGS, Binding("ctrl+s", "save", "Save", priority=True)]
    DEFAULT_CSS = "SettingsScreen TextArea { height: 9; border: round $primary; }"

    def __init__(self, settings: Settings) -> None:
        """Initialize inputs from the current application preferences."""
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        """Keep all preferences and disclosure reachable by Tab and Shift+Tab."""
        with VerticalScroll():
            yield Label("Settings · Ctrl+S saves · Escape cancels")
            yield Label("Theme · Oracle TUI palettes")
            yield Select(
                [(name, name) for name in THEME_NAMES],
                value=self.settings.theme,
                allow_blank=False,
                id="theme",
            )
            yield Label("OpenAI model ID")
            yield Input(self.settings.model, id="model")
            yield Label("Reasoning effort · default omits the parameter")
            yield Select(
                [(x, x) for x in ("default", "none", "low", "medium", "high", "xhigh", "max")],
                value=self.settings.reasoning,
                allow_blank=False,
                id="reasoning",
            )
            yield Label("Fast mode · paid service tier")
            yield Switch(self.settings.fast, id="fast")
            yield Label("Custom system prompt")
            yield TextArea(self.settings.system_prompt, id="system-prompt", tab_behavior="focus")
            yield Static(
                "Credentials come from the configured AWS secret, or OPENAI_API_KEY when no secret is configured. Restart after changing credential references in .env. Prompts send accessible schema metadata, prompt context and current SQL to OpenAI, never query results. First table previews also send that table's metadata to infer and cache a browsing recipe. Responses are not requested for server-side storage.",
                classes="hint",
            )
            yield Static("", id="settings-error", classes="error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def action_save(self) -> None:
        """Return validated settings; persistence belongs to the application."""
        settings = Settings(
            self.query_one("#model", Input).value,
            str(self.query_one("#reasoning", Select).value),
            self.query_one("#fast", Switch).value,
            self.query_one("#system-prompt", TextArea).text,
            theme=str(self.query_one("#theme", Select).value),
        )
        try:
            settings.validate()
        except ValueError as error:
            self.query_one("#settings-error", Static).update(str(error))
            return
        self.dismiss(settings)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        """Route keyboard-activated Save and Cancel buttons."""
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)


class ConfirmScreen(Dialog):
    """Require an intentional confirmation for discarded drafts or enabling writes."""

    def __init__(self, question: str) -> None:
        """Set the exact operation and target visible at the confirmation seam."""
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        """Default keyboard focus to Cancel, not the effectful operation."""
        with Vertical():
            yield Static(self.question, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="warning")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        """Return True only for the explicit confirm control."""
        self.dismiss(event.button.id == "confirm")


class ExportScreen(Dialog):
    """Choose an exclusive-create CSV destination with visible truncation and NULL semantics."""

    def __init__(self, truncated: bool) -> None:
        """Keep export warnings tied to the result being exported."""
        super().__init__()
        self.truncated = truncated

    def compose(self) -> ComposeResult:
        """Offer path entry without guessing a confidential-data destination."""
        with Vertical():
            yield Label("Export retained result to CSV")
            yield Static(
                "Only displayed rows will be exported (result truncated)."
                if self.truncated
                else "All retained rows will be exported.",
                classes="hint",
            )
            yield Static(
                "UTF-8 · SQL NULL becomes an empty field · existing files are never overwritten. Treat spreadsheet formulas in data as untrusted.",
                classes="hint",
            )
            yield Input(placeholder="Destination CSV path", id="export-path")
            with Horizontal(classes="buttons"):
                yield Button("Export", id="export", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    def submitted(self) -> None:
        """Accept an explicit nonempty export path."""
        value = self.query_one("#export-path", Input).value.strip()
        if value:
            self.dismiss(Path(value).expanduser())

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        """Activate export or cancel with the keyboard."""
        if event.button.id == "export":
            self.submitted()
        else:
            self.dismiss(None)


HELP = """PGDesk · keyboard reference

Ctrl+N        Open cluster/database chooser (reloads config)
Ctrl+D        Duplicate current workspace (independent pool/editor/results)
Ctrl+W        Close workspace (confirms draft/running work)
[ / ]         Previous/next workspace outside text inputs
Ctrl+PageUp/Down  Previous/next workspace from anywhere
Tab / Shift+Tab   Move focus; editor Tab also moves focus
Escape        Leave input/editor for schema navigation; dismiss dialog
/             Filter schema/table/view tree outside text inputs
Enter         Expand tree; on table, automatically run a light 5-row preview
F2 / F3 / F4  Show/focus AI / SQL / results; repeat while focused to hide
Ctrl+B        Toggle schema sidebar
Ctrl+Enter / F5   Run selected SQL, otherwise entire editor (one statement)
F6            Cancel running query (never automatically retried)
F7            Refresh schema metadata
F8            Toggle manual read-only/write mode (writes require confirmation)
F9            Settings: Oracle theme, model, reasoning, Fast mode, prompt
Ctrl+L        Run light latest 100 for the selected table
Ctrl+G        Run heavy latest 100 (SELECT *) for the selected table
Ctrl+S        Export retained result as CSV (no overwrite)
Ctrl+K        Clear current tab's AI prompt context
F1 / ?        This help
Ctrl+Q        Quit; confirms unsaved work

AI prompt: Enter inserts a SQL-only reply into the editor; F5 executes it.
SQL editor: Enter inserts a newline. Automatic previews confirm before replacing manual SQL.
First table use asks AI for useful columns and a descending recency key,
then caches the recipe for this app session. F7 refreshes metadata and recipes.
Light previews select up to 8 columns: text capped at 160 characters,
large/structured fields replaced by __is_null flags. Heavy retrieves full values.
Automatic previews are read-only, with 2s statement and 200ms lock deadlines.
If ordering is expensive, an unordered sample is explicitly marked NOT LATEST.
Views, foreign tables and expensive sample plans require explicit manual SQL/F5.
Duplicating copies selection, SQL and prompt context, not results or active work.
All buttons, lists, tabs and table cells are keyboard navigable.
Enter / click on a result cell opens retained-value detail: pretty/raw text,
collapsible JSON tree, Ctrl+F search, F3/Shift+F3 next/previous match,
Ctrl+Y copy all (terminal clipboard support required), Ctrl+S export to an
explicit file without overwriting. Escape closes. Tree labels abbreviate long
leaves; switch to pretty/raw to read or search their full text. Light results
may contain capped text/NULL flags: close and use Ctrl+G before inspecting full values.
Run commits on success, rolls back on failure. No manual transaction,
SET/RESET or COPY session state; pooled runs are isolated. A lost connection
near commit has an uncertain outcome: inspect before rerunning writes.
Rows are retained up to row_limit (default 10,000). The driver still receives
the full result: use LIMIT for large queries. CSV exports retained rows only.
SQL NULL is shown as NULL; CSV uses an empty field. Payload counters estimate
SQL text sent and retained cell text received, not PostgreSQL wire traffic.
"""


class HelpScreen(Dialog):
    """Scrollable in-application reference for every action and execution safety rule."""

    def compose(self) -> ComposeResult:
        """Display plain text so help remains readable without mouse interaction."""
        with VerticalScroll():
            yield Static(HELP)
            yield Button("Close", id="close")

    @on(Button.Pressed)
    def close_pressed(self) -> None:
        """Dismiss help without affecting workspace state."""
        self.dismiss(None)
