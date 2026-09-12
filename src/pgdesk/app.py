"""Textual application shell: keyboard routing, tab lifetime and live pool status."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.widgets import Footer, Header, Input, Static, TabbedContent, TabPane, TextArea, Tree

from pgdesk.ai import SqlAssistant
from pgdesk.browsing import BrowsePlanner
from pgdesk.config import Config, Settings, load_config, load_settings, save_settings
from pgdesk.screens import ConfirmScreen, ConnectScreen, HelpScreen, SettingsScreen
from pgdesk.themes import THEME_NAMES, build_theme
from pgdesk.workspace import Workspace


class PgDesk(App):
    """Manage independently connected workspaces; database work stays inside each tab."""

    TITLE = "PGDesk"
    SUB_TITLE = "PostgreSQL workspaces"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; color: $foreground; }
    Header { background: $header_bg; color: $header_fg; }
    #pool-status { height: 2; padding: 0 1; background: $panel; color: $foreground; }
    #workspaces { height: 1fr; }
    TabPane { padding: 0; }
    #welcome { height: 1fr; padding: 2 4; content-align: center middle; }
    Footer { background: $header_bg; color: $header_fg; }
    Input { background: $input_bg; border: tall $input_border; }
    DataTable { background: $surface; }
    DataTable > .datatable--header { background: $header_bg; color: $header_fg; }
    DataTable > .datatable--odd-row { background: $odd_row_bg; }
    DataTable > .datatable--cursor { background: $cursor_bg; color: $foreground; }
    Tree > .tree--cursor { background: $cursor_bg; color: $foreground; }
    """
    BINDINGS: ClassVar = [
        Binding("ctrl+n", "connect", "Connect", priority=True),
        Binding("ctrl+w", "close_tab", "Close tab", priority=True),
        Binding("ctrl+pageup", "step_tab(-1)", "Previous tab", show=False, priority=True),
        Binding("ctrl+pagedown", "step_tab(1)", "Next tab", show=False, priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("f2", "panel('ai')", "AI", priority=True),
        Binding("f3", "panel('sql')", "SQL", priority=True),
        Binding("f4", "panel('data')", "Results", priority=True),
        Binding("ctrl+enter,f5", "run_query", "Run", priority=True),
        Binding("f6", "cancel_query", "Cancel", priority=True),
        Binding("f7", "refresh_schema", "Schema", show=False, priority=True),
        Binding("f8", "toggle_writes", "Read/write", show=False, priority=True),
        Binding("f9", "settings", "Settings", priority=True),
        Binding("ctrl+b", "sidebar", "Tree", show=False, priority=True),
        Binding("ctrl+l", "light_preview", "Light 100", priority=True),
        Binding("ctrl+g", "heavy_preview", "Heavy 100", priority=True),
        Binding("ctrl+d", "duplicate_tab", "Duplicate", show=False, priority=True),
        Binding("ctrl+k", "clear_ai", "Clear AI", show=False, priority=True),
        Binding("ctrl+s", "export", "CSV", priority=True),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
    ]
    WORKSPACE_ACTIONS = frozenset(binding.action.split("(")[0] for binding in BINDINGS)

    def __init__(
        self,
        config: Config,
        config_path: Path,
        settings: Settings | None = None,
        assistant: SqlAssistant | None = None,
    ) -> None:
        """Own shared preferences/AI transport, never shared database sessions."""
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.settings = settings or load_settings(config.settings_path)
        for name in THEME_NAMES:
            self.register_theme(build_theme(name))
        self.theme = self.settings.theme
        self.assistant = assistant or SqlAssistant(
            secret=config.openai_secret, key_path=config.openai_key_path
        )
        self.browsing = BrowsePlanner(self.assistant)
        self.workspaces: dict[str, Workspace] = {}
        self.serial = 0
        self.quitting = False

    def compose(self) -> ComposeResult:
        """Place database tabs above workspace content and health immediately below the title."""
        yield Header(show_clock=True)
        yield Static(
            "No database connected · Ctrl+N opens a workspace", id="pool-status", markup=False
        )
        yield TabbedContent(id="workspaces")
        yield Static(
            "PGDesk\n\nCtrl+N  Open a cluster/database workspace\nF9  AI settings     F1  Keyboard reference\n\nIndependent pools · Read-only by default · AI drafts, you execute\n\nConfigure cluster and AWS secret references in .env\nusing .env.example. Secret values are fetched at runtime.",
            id="welcome",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Start a cheap status redraw, with all health I/O owned by pool threads."""
        self.query_one("#workspaces").display = False
        self.set_interval(1, self.refresh_status)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Do not let global priority hotkeys mutate hidden workspaces behind a modal."""
        return not (len(self.screen_stack) > 1 and action in self.WORKSPACE_ACTIONS)

    @property
    def active_workspace(self) -> Workspace | None:
        """Resolve the active tab without depending on widget focus or stale callbacks."""
        return self.workspaces.get(self.query_one("#workspaces", TabbedContent).active)

    def refresh_status(self) -> None:
        """Render real health, connection age and explicitly estimated payload counters."""
        workspace = self.active_workspace
        if workspace is None:
            self.query_one("#pool-status", Static).update(
                "No database connected · Ctrl+N opens a workspace"
            )
            return
        status = workspace.session.status()
        mode = "READ ONLY" if workspace.read_only else "WRITES ENABLED"
        production = " [PROD]" if workspace.cluster.production else ""
        check = (
            "never"
            if status.last_check_seconds is None
            else f"{status.last_check_seconds:.0f}s ago"
        )
        self.query_one("#pool-status", Static).update(
            f"{workspace.cluster.name}/{workspace.database}{production} · {mode} · {status.state} · live {status.live}/{status.target} · idle {status.idle} · oldest {status.oldest_seconds:.0f}s · errors {status.errors}\n"
            f"Tab uptime {status.uptime_seconds:.0f}s · checked {check} · payload estimate sent {status.sent_bytes / 1_000_000:.3f} MB / received {status.received_bytes / 1_000_000:.3f} MB · {len(self.workspaces)} workspaces"
        )

    def action_connect(self) -> None:
        """Reload configuration before opening the searchable cluster/database chooser."""
        try:
            self.config = replace(
                load_config(self.config_path), settings_path=self.config.settings_path
            )
        except (ValueError, TypeError, OSError) as error:
            self.notify(
                f"Configuration invalid: {type(error).__name__}. Check {self.config_path}.",
                severity="error",
            )
            return
        self.push_screen(ConnectScreen(self.config, self.config_path), self._chosen)

    async def _chosen(self, pair) -> None:
        """Open only a concretely selected cluster/database, retaining existing duplicates."""
        if pair:
            await self.open_workspace(*pair)

    async def open_workspace(self, cluster, database: str) -> Workspace:
        """Focus an existing pair or mount a pool-owning workspace; duplicates are explicit."""
        tabs = self.query_one("#workspaces", TabbedContent)
        for tab_id, workspace in self.workspaces.items():
            if workspace.cluster.name == cluster.name and workspace.database == database:
                self.set_focus(None)
                tabs.active = tab_id
                workspace.query_one("#schema-tree", Tree).focus()
                return workspace
        return await self._mount_workspace(cluster, database)

    async def duplicate_workspace(self, source: Workspace) -> Workspace:
        """Fork the selected table and SQL into an independent, initially read-only workspace."""
        return await self._mount_workspace(source.cluster, source.database, source=source)

    async def _mount_workspace(
        self, cluster, database: str, *, source: Workspace | None = None
    ) -> Workspace:
        """Own mounting/cleanup for both chooser-created and deliberately duplicated tabs."""
        tabs = self.query_one("#workspaces", TabbedContent)
        self.serial += 1
        tab_id = f"db-{self.serial}"
        workspace = Workspace(
            cluster, database, source.config if source else self.config, self.assistant
        )
        pane = TabPane(
            Content(
                f"{cluster.name}/{database}"
                + (" [PROD]" if cluster.production else "")
                + (f" · copy {self.serial}" if source else "")
            ),
            workspace,
            id=tab_id,
        )
        self.workspaces[tab_id] = workspace
        self.query_one("#welcome").display = False
        tabs.display = True
        try:
            await tabs.add_pane(pane)
        except Exception:
            self.workspaces.pop(tab_id)
            await asyncio.to_thread(workspace.session.close)
            raise
        tabs.active = tab_id
        if source:
            workspace.copy_from(source)
            workspace.query_one("#sql-editor", TextArea).focus()
        else:
            workspace.query_one("#schema-tree", Tree).focus()
        self.refresh_status()
        return workspace

    def action_step_tab(self, direction: int) -> None:
        """Wrap tab navigation in insertion order, preserving each editor and result."""
        if not self.workspaces:
            return
        tabs = self.query_one("#workspaces", TabbedContent)
        ids = list(self.workspaces)
        self.set_focus(None)
        tabs.active = ids[(ids.index(tabs.active) + direction) % len(ids)]
        self.workspaces[tabs.active].query_one("#schema-tree", Tree).focus()
        self.refresh_status()

    def action_close_tab(self) -> None:
        """Capture the exact workspace before confirming lost work or running operations."""
        workspace = self.active_workspace
        if workspace is None or workspace.closing:
            return
        tab_id = next(key for key, value in self.workspaces.items() if value is workspace)
        if workspace.has_draft or (workspace.query_task and not workspace.query_task.done()):
            self.push_screen(
                ConfirmScreen(
                    f"Close {workspace.cluster.name}/{workspace.database}? In-memory drafts/results/chat will be discarded and running queries cancelled."
                ),
                lambda yes: self.run_worker(self.close_workspace(tab_id)) if yes else None,
            )
        else:
            self.run_worker(self.close_workspace(tab_id))

    async def close_workspace(self, tab_id: str) -> None:
        """Await cancellation/resource disposal before removing tab widgets."""
        workspace = self.workspaces.get(tab_id)
        if workspace is None or workspace.closing:
            return
        await workspace.shutdown()
        self.workspaces.pop(tab_id, None)
        tabs = self.query_one("#workspaces", TabbedContent)
        await tabs.remove_pane(tab_id)
        tabs.display = bool(self.workspaces)
        self.query_one("#welcome").display = not self.workspaces
        self.refresh_status()

    def action_panel(self, name: str) -> None:
        """Show/focus one panel, or hide it when its primary widget already has focus."""
        if workspace := self.active_workspace:
            workspace.toggle_panel(name)

    def action_sidebar(self) -> None:
        """Show or hide the schema tree without discarding selection or metadata."""
        if workspace := self.active_workspace:
            sidebar = workspace.query_one(".sidebar")
            sidebar.display = not sidebar.display

    def action_run_query(self) -> None:
        """Forward execution to the active tab's captured SQL and mode."""
        if workspace := self.active_workspace:
            workspace.run_query()

    async def action_cancel_query(self) -> None:
        """Cancel only the active workspace's user query."""
        if workspace := self.active_workspace:
            await workspace.cancel_query()

    def action_refresh_schema(self) -> None:
        """Refresh the active tab's tree and AI metadata snapshot."""
        if workspace := self.active_workspace:
            workspace.refresh_catalog(replan=True)

    def action_toggle_writes(self) -> None:
        """Use the tab's explicit write-mode confirmation path."""
        if workspace := self.active_workspace:
            workspace.toggle_writes()

    def action_light_preview(self) -> None:
        """Refresh the selected relation with a narrow hundred-row read."""
        if workspace := self.active_workspace:
            workspace.browse(limit=100)

    def action_heavy_preview(self) -> None:
        """Refresh the selected relation with all columns and a hundred-row cap."""
        if workspace := self.active_workspace:
            workspace.browse(limit=100, heavy=True)

    async def action_duplicate_tab(self) -> None:
        """Create a separate editable workspace without executing the copied SQL."""
        if workspace := self.active_workspace:
            await self.duplicate_workspace(workspace)

    def action_clear_ai(self) -> None:
        """Forget only the active workspace's in-memory AI conversation."""
        if workspace := self.active_workspace:
            workspace.clear_ai()

    def action_export(self) -> None:
        """Open an export path dialog for the active retained result."""
        if workspace := self.active_workspace:
            workspace.export()

    def action_settings(self) -> None:
        """Edit global AI defaults without changing any in-flight request."""
        self.push_screen(SettingsScreen(self.settings), self._save_settings)

    def _save_settings(self, settings: Settings | None) -> None:
        """Publish preferences only after a successful atomic save."""
        if settings is None:
            return
        try:
            save_settings(self.config.settings_path, settings)
        except OSError:
            self.notify(
                "Could not save AI settings; check configuration-directory permissions",
                severity="error",
            )
            return
        self.settings = settings
        self.theme = settings.theme
        for workspace in self.workspaces.values():
            workspace.apply_theme()
        self.notify("Settings saved")

    def action_help(self) -> None:
        """Show the complete keyboard and execution contract reference."""
        self.push_screen(HelpScreen())

    def action_request_quit(self) -> None:
        """Confirm lost work, then run the same resource shutdown path as tab closure."""
        if self.quitting:
            return
        if any(
            workspace.has_draft or (workspace.query_task and not workspace.query_task.done())
            for workspace in self.workspaces.values()
        ):
            self.push_screen(
                ConfirmScreen(
                    "Quit PGDesk? All in-memory SQL drafts/results/chat will be discarded and running queries cancelled."
                ),
                lambda yes: self.run_worker(self.shutdown_and_exit()) if yes else None,
            )
        else:
            self.run_worker(self.shutdown_and_exit())

    async def shutdown_and_exit(self) -> None:
        """Await every workspace and the AI transport before ending the terminal session."""
        self.quitting = True
        await asyncio.gather(*(workspace.shutdown() for workspace in self.workspaces.values()))
        await self.browsing.close()
        await self.assistant.close()
        self.exit()

    async def on_unmount(self) -> None:
        """Also release resources on framework exit, interrupt or an unhandled UI error."""
        await asyncio.gather(*(workspace.shutdown() for workspace in self.workspaces.values()))
        await self.browsing.close()
        await self.assistant.close()

    async def on_key(self, event: events.Key) -> None:
        """Keep Oracle's navigation keys outside editors, preserving normal typing inside."""
        if len(self.screen_stack) > 1:
            return
        editing = isinstance(self.focused, (Input, TextArea))
        if event.key == "escape" and (workspace := self.active_workspace):
            workspace.query_one(".sidebar").display = True
            workspace.query_one("#schema-tree", Tree).focus()
        elif not editing and event.key in {"left_square_bracket", "right_square_bracket", "[", "]"}:
            self.action_step_tab(1 if event.key in {"right_square_bracket", "]"} else -1)
        elif not editing and event.key in {"slash", "/"} and (workspace := self.active_workspace):
            workspace.query_one(".sidebar").display = True
            workspace.query_one("#tree-filter", Input).focus()
        elif not editing and event.key in {"question_mark", "?"}:
            self.action_help()
        else:
            return
        event.prevent_default()
        event.stop()
