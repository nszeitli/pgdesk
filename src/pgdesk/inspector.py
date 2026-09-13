"""Read-only inspection of retained cells; never fetch data or mutate query results."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Select, Static, TextArea, Tree
from textual.widgets.text_area import Selection
from textual.widgets.tree import TreeNode

from pgdesk.catalog import cell_text
from pgdesk.screens import Dialog
from pgdesk.themes import editor_theme

# Highlighting is synchronous; cap decoration work, never the retained cell text.
MAX_HIGHLIGHT_CHARACTERS = 200_000
MAX_TREE_CHILDREN = 1_000


def export_value(path: Path, text: str) -> None:
    """Create an owner-only UTF-8 value file without replacing any existing destination."""
    fd = os.open(path.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


class ValueInspector(Dialog):
    """Own a cell snapshot with full text, lazy JSON branches and explicit copy/export actions."""

    BINDINGS: ClassVar = [
        *Dialog.BINDINGS,
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("f3", "next_match", "Next match", priority=True),
        Binding("shift+f3", "previous_match", "Previous match", priority=True),
        Binding("ctrl+y", "copy_value", "Copy value", priority=True),
        Binding("ctrl+s", "export_value", "Export value", priority=True),
    ]
    DEFAULT_CSS = """
    ValueInspector > Vertical { width: 95%; height: 92%; padding: 0 1; }
    ValueInspector .toolbar { height: 3; }
    ValueInspector .toolbar Button { min-width: 9; }
    ValueInspector #inspect-view { width: 22; }
    ValueInspector #inspect-text, ValueInspector #inspect-tree { height: 1fr; }
    ValueInspector #inspect-notice, ValueInspector #inspect-status { height: auto; max-height: 3; }
    ValueInspector #inspect-notice { color: $warning; }
    """

    def __init__(self, column: str, row: int, value: object, *, light: bool = False) -> None:
        """Capture a retained value, not the clipped grid text or a mutable workspace selection."""
        super().__init__()
        self.column = column
        self.row = row
        self.value = value
        self.light = light
        self.structured = isinstance(value, (dict, list))
        self.mode = "pretty" if self.structured else "raw"
        self._texts: dict[str, str] = {}
        self._populated: set[int] = set()
        self._match = -1
        self._needle = ""

    def value_text(self, mode: str | None = None) -> str:
        """Render the complete retained value; tree copy/export uses the full pretty representation."""
        mode = mode or self.mode
        mode = "pretty" if mode == "tree" else mode
        if mode not in self._texts:
            self._texts[mode] = (
                json.dumps(self.value, ensure_ascii=False, indent=2, default=str)
                if mode == "pretty" and self.structured
                else cell_text(self.value)
            )
        return self._texts[mode]

    def compose(self) -> ComposeResult:
        """Keep view, search, clipboard and explicit export destination keyboard accessible."""
        text = self.value_text()
        with Vertical():
            yield Static(Text(f"Cell detail · row {self.row + 1} · {self.column} · Escape closes"))
            yield Static(
                "LIGHT PREVIEW: text may be capped and large fields are only __is_null flags. "
                "Close and press Ctrl+G, then inspect the heavy result for the full source value."
                if self.light
                else "Retained value · grid clipping does not apply here · no database query",
                id="inspect-notice",
                markup=False,
            )
            with Horizontal(classes="toolbar"):
                options = [("Raw", "raw")]
                if self.structured:
                    options = [("Pretty JSON", "pretty"), ("Raw", "raw"), ("JSON tree", "tree")]
                yield Select(options, value=self.mode, allow_blank=False, id="inspect-view")
                yield Button("Copy all", id="inspect-copy")
                yield Button("Close", id="inspect-close")
            with Horizontal(classes="toolbar"):
                yield Input(
                    placeholder="Case-sensitive search · Enter / F3 next · Shift+F3 previous",
                    id="inspect-search",
                )
                yield Button("Previous", id="inspect-previous")
                yield Button("Next", id="inspect-next")
            yield TextArea(
                text,
                language="json"
                if self.structured and len(text) <= MAX_HIGHLIGHT_CHARACTERS
                else None,
                read_only=True,
                show_line_numbers=True,
                soft_wrap=False,
                tab_behavior="focus",
                id="inspect-text",
            )
            yield Tree(Text("$"), data=self.value, id="inspect-tree")
            with Horizontal(classes="toolbar"):
                yield Input(
                    placeholder="Explicit export path · UTF-8 · no overwrite", id="inspect-path"
                )
                yield Button("Export", id="inspect-export")
            yield Static(
                "Copy all uses the terminal clipboard (OSC52). Export writes the entire current text view.",
                id="inspect-status",
                markup=False,
            )

    def on_mount(self) -> None:
        """Apply the current palette without allocating hidden JSON tree branches."""
        editor = self.query_one("#inspect-text", TextArea)
        theme = editor_theme(self.app.settings.theme)
        editor.register_theme(theme)
        editor.theme = theme.name
        tree = self.query_one("#inspect-tree", Tree)
        tree.display = False
        editor.focus()

    def _populate(self, node: TreeNode) -> None:
        """Bound each expansion; overflow remains available in complete pretty/raw text."""
        if node.id in self._populated:
            return
        self._populated.add(node.id)
        value = node.data
        entries = (
            value.items()
            if isinstance(value, dict)
            else enumerate(value)
            if isinstance(value, list)
            else ()
        )
        for index, (key, child) in enumerate(entries):
            if index == MAX_TREE_CHILDREN:
                node.add_leaf(
                    Text(
                        f"… {len(value) - MAX_TREE_CHILDREN:,} more entries · use Pretty/Raw or Ctrl+F"
                    )
                )
                break
            name = json.dumps(key, ensure_ascii=False)
            if isinstance(child, (dict, list)) and child:
                kind = "object" if isinstance(child, dict) else "array"
                node.add(Text(f"{name}: {kind} ({len(child)})"), data=child)
            else:
                rendered = json.dumps(child, ensure_ascii=False, default=str)
                node.add_leaf(
                    Text(f"{name}: {rendered[:160]}" + ("…" if len(rendered) > 160 else "")),
                    data=child,
                )

    @on(Tree.NodeExpanded, "#inspect-tree")
    def expand_json(self, event: Tree.NodeExpanded) -> None:
        """Load children on demand; collapsing and reopening never duplicates them."""
        if self.mode == "tree":
            self._populate(event.node)

    @on(Select.Changed, "#inspect-view")
    def view_changed(self, event: Select.Changed) -> None:
        """Switch representation without editing or re-querying the captured value."""
        mode = str(event.value)
        if mode == self.mode:
            return
        self.mode = mode
        self._match = -1
        editor = self.query_one("#inspect-text", TextArea)
        tree = self.query_one("#inspect-tree", Tree)
        editor.display = mode != "tree"
        tree.display = mode == "tree"
        if mode == "tree":
            self._populate(tree.root)
            tree.root.expand()
            tree.focus()
        else:
            editor.load_text(self.value_text())
            editor.focus()

    def action_find(self) -> None:
        """Focus the explicit search field without altering the value."""
        self.query_one("#inspect-search", Input).focus()

    @on(Input.Submitted, "#inspect-search")
    def action_next_match(self) -> None:
        """Find the next exact match, wrapping at the end of the full text."""
        self._find_match(False)

    def action_previous_match(self) -> None:
        """Find the previous exact match, wrapping at the beginning."""
        self._find_match(True)

    def _find_match(self, backwards: bool) -> None:
        """Map Python string offsets to TextArea coordinates without Unicode casefold drift."""
        needle = self.query_one("#inspect-search", Input).value
        if not needle:
            self.action_find()
            return
        if self.mode == "tree":
            self.query_one("#inspect-view", Select).value = "pretty"
            self.mode = "pretty"
            self.query_one("#inspect-tree").display = False
            self.query_one("#inspect-text", TextArea).display = True
            self.query_one("#inspect-text", TextArea).load_text(self.value_text())
            self._match = -1
        text = self.value_text()
        if needle != self._needle:
            self._match = -1
            self._needle = needle
        if backwards:
            found = text.rfind(needle, 0, self._match) if self._match >= 0 else -1
            if found < 0:
                found = text.rfind(needle)
        else:
            found = text.find(needle, self._match + 1)
            if found < 0:
                found = text.find(needle)
        self._match = found
        status = self.query_one("#inspect-status", Static)
        if found < 0:
            status.update("No match in the complete retained text.")
            return
        end = found + len(needle)
        start_location = (text.count("\n", 0, found), found - text.rfind("\n", 0, found) - 1)
        end_location = (text.count("\n", 0, end), end - text.rfind("\n", 0, end) - 1)
        editor = self.query_one("#inspect-text", TextArea)
        editor.selection = Selection(start_location, end_location)
        editor.scroll_cursor_visible(center=True)
        status.update(
            f"Match at line {start_location[0] + 1} · search wraps · F3 next / Shift+F3 previous"
        )

    def action_copy_value(self) -> None:
        """Request an explicit whole-value clipboard copy through the terminal's OSC52 support."""
        self.app.copy_to_clipboard(self.value_text())
        self.query_one("#inspect-status", Static).update(
            "Copy requested for the entire value; terminal clipboard support is required."
        )

    @on(Input.Submitted, "#inspect-path")
    async def action_export_value(self) -> None:
        """Write only to the chosen destination and report failures without overwriting files."""
        path = self.query_one("#inspect-path", Input).value.strip()
        if not path:
            self.query_one("#inspect-path", Input).focus()
            return
        text = self.value_text()
        try:
            await asyncio.to_thread(export_value, Path(path), text)
        except (OSError, ValueError) as error:
            message = (
                f"Export failed: {type(error).__name__}. Check the path or choose a new filename."
            )
        else:
            message = f"Exported the entire value to {path}"
        self.query_one("#inspect-status", Static).update(message)

    @on(Button.Pressed)
    async def pressed(self, event: Button.Pressed) -> None:
        """Route mouse and keyboard button activation to the same inspector operations."""
        event.stop()
        match event.button.id:
            case "inspect-close":
                self.dismiss(None)
            case "inspect-copy":
                self.action_copy_value()
            case "inspect-export":
                await self.action_export_value()
            case "inspect-next":
                self.action_next_match()
            case "inspect-previous":
                self.action_previous_match()
