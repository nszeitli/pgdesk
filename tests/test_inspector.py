"""Retained-value rendering and explicit export safety regressions."""

import json
import stat
from pathlib import Path

import pytest

from pgdesk.inspector import ValueInspector, export_value


def test_json_views_preserve_nested_values_beyond_grid_clipping() -> None:
    """Pretty/raw views and tree export preserve Unicode, escapes, nulls and late payload content."""
    value = {"items": [{"text": 'é\n\\"' * 900, "nullable": None, "enabled": False}], "empty": []}
    inspector = ValueInspector("payload", 0, value)
    for mode in ("raw", "pretty", "tree"):
        assert json.loads(inspector.value_text(mode)) == value
    assert "\n" in inspector.value_text("pretty")


def test_export_preserves_text_and_never_overwrites(tmp_path: Path) -> None:
    """Full-value exports are owner-only, preserve Unicode/newlines and refuse path collisions."""
    path = tmp_path / "value.txt"
    text = "é\r\n" + "long text\n" * 900
    export_value(path, text)
    assert path.read_bytes() == text.encode("utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        export_value(path, "replacement")
    assert path.read_bytes() == text.encode("utf-8")
