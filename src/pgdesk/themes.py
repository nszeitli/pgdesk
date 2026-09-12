"""Self-contained Oracle TUI palette snapshot, including its OMP-derived color schemes."""

from __future__ import annotations

import json
from importlib.resources import files

from rich.style import Style
from textual.theme import Theme
from textual.widgets.text_area import TextAreaTheme

_PALETTES: dict[str, dict[str, str]] = json.loads(
    files("pgdesk").joinpath("themes.json").read_text()
)
THEME_NAMES = tuple(_PALETTES)
DEFAULT_THEME = THEME_NAMES[0]
_THEME_FIELDS = (
    "primary",
    "secondary",
    "warning",
    "error",
    "success",
    "accent",
    "foreground",
    "background",
    "surface",
    "panel",
)


def build_theme(name: str) -> Theme:
    """Map the copied Oracle roles to the same Textual theme fields and CSS variables."""
    roles = _PALETTES[name]
    return Theme(
        name=name,
        **{field: roles[field] for field in _THEME_FIELDS},
        dark=name != "light",
        variables={role: color for role, color in roles.items() if role not in _THEME_FIELDS},
    )


def editor_theme(name: str) -> TextAreaTheme:
    """Keep SQL text, cursor and syntax readable in both dark and light application palettes."""
    roles = _PALETTES[name]
    return TextAreaTheme(
        name="pgdesk-" + name,
        base_style=Style(color=roles["foreground"], bgcolor=roles["input_bg"]),
        gutter_style=Style(color=roles["secondary"], bgcolor=roles["input_bg"]),
        cursor_style=Style(color=roles["background"], bgcolor=roles["foreground"]),
        cursor_line_style=Style(bgcolor=roles["panel"]),
        selection_style=Style(bgcolor=roles["cursor_bg"]),
        syntax_styles={
            "keyword": Style(color=roles["accent"], bold=True),
            "type": Style(color=roles["secondary"]),
            "function": Style(color=roles["primary"]),
            "function.call": Style(color=roles["primary"]),
            "number": Style(color=roles["warning"]),
            "float": Style(color=roles["warning"]),
            "string": Style(color=roles["success"]),
            "comment": Style(color=roles["secondary"], italic=True),
            "operator": Style(color=roles["foreground"]),
        },
    )
