"""Regressions for credential boundaries, preference persistence and exact retained CSV data."""

import csv
import os
from pathlib import Path

import pytest

import pgdesk.__main__ as entrypoint
from pgdesk.catalog import QueryResult, Relation
from pgdesk.config import Settings, load_config, load_settings, save_settings


@pytest.mark.parametrize(
    "entry",
    [
        "PGDESK_CLUSTERS=a,b\nPGDESK_A_NAME=duplicate\nPGDESK_A_SERVICE=a\nPGDESK_B_NAME=duplicate\nPGDESK_B_SERVICE=b",
        "PGDESK_CLUSTERS=a\nPGDESK_A_SERVICE=a\nPGDESK_A_DSN_ENV=DSN",
        "PGDESK_CLUSTERS=a\nPGDESK_A_DSN=postgresql://not-a-real-secret",
        "PGDESK_CLUSTERS=a",
        "PGDESK_CLUSTERS=openai\nPGDESK_OPENAI_SERVICE=database",
    ],
)
def test_invalid_cluster_references_are_rejected(tmp_path: Path, entry: str) -> None:
    """Ambiguous credentials and duplicate identities must fail before opening any pool."""
    path = tmp_path / ".env"
    path.write_text(entry)
    with pytest.raises((ValueError, TypeError)):
        load_config(path)


def test_dotenv_references_do_not_inherit_or_mutate_process_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    """A shell's unrelated cluster settings cannot retarget the configured database account."""
    monkeypatch.setenv("PGDESK_CLUSTERS", "ambient")
    monkeypatch.setenv("UNRELATED_SECRET_NAME", "wrong-account-secret")
    path = tmp_path / ".env"
    path.write_text(
        "PGDESK_CLUSTERS=chosen\nPGDESK_CHOSEN_AWS_PROFILE=test-profile\n"
        "PGDESK_CHOSEN_AWS_REGION=us-west-2\nPGDESK_CHOSEN_SECRET_ID=${UNRELATED_SECRET_NAME}\n"
    )
    config = load_config(path)
    assert config.clusters[0].aws_secret.secret_id != os.environ["UNRELATED_SECRET_NAME"]
    assert os.environ["PGDESK_CLUSTERS"] == "ambient"


@pytest.mark.parametrize("layout", ["repo/src", "venv/site-packages"])
def test_default_config_is_anchored_to_installation_not_launch_directory(
    tmp_path: Path, monkeypatch, layout: str
) -> None:
    """Launching from another project cannot select that project's unrelated credentials."""
    package_parent = tmp_path / layout
    monkeypatch.setattr(entrypoint, "__file__", str(package_parent / "pgdesk" / "__main__.py"))
    monkeypatch.setattr(entrypoint, "CONFIG_DIR", tmp_path / "user-config")
    expected = (
        package_parent.parent / ".env"
        if layout == "repo/src"
        else tmp_path / "user-config" / ".env"
    )
    elsewhere = tmp_path / "unrelated-project"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("PGDESK_CLUSTERS=unrelated\n")
    monkeypatch.chdir(elsewhere)
    assert entrypoint._default_config_path() == expected


def test_bad_settings_do_not_replace_last_working_preferences(tmp_path: Path) -> None:
    """A rejected edit cannot erase previously usable settings or change file permissions."""
    path = tmp_path / "settings.json"
    accepted = Settings(
        model="test-model", reasoning="low", fast=False, system_prompt="Custom SQL policy"
    )
    save_settings(path, accepted)
    with pytest.raises(ValueError):
        save_settings(path, Settings(model=""))
    assert load_settings(path) == accepted
    assert path.stat().st_mode & 0o077 == 0


def test_csv_roundtrip_and_exclusive_create(tmp_path: Path) -> None:
    """Quotes, Unicode, newlines and NULL survive the documented CSV mapping; no overwrite."""
    result = QueryResult(
        ("name", "value"), (('quoted,"雪"\nnext', 42), (None, "")), "SELECT 2", 0.1, False
    )
    path = tmp_path / "result.csv"
    result.export_csv(path)
    assert path.stat().st_mode & 0o077 == 0
    with path.open(newline="", encoding="utf-8") as stream:
        assert list(csv.reader(stream)) == [
            ["name", "value"],
            ['quoted,"雪"\nnext', "42"],
            ["", ""],
        ]
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        QueryResult(("other",), ((1,),), "", 0, False).export_csv(path)
    assert path.read_bytes() == original


def test_quoted_relation_preview_is_one_statement() -> None:
    """Unusual database identifiers must remain identifiers rather than executable text."""
    from pglast import parse_sql

    relation = Relation('schema"odd', 'table"; DROP TABLE victim;--', "r", ())
    parsed = parse_sql(relation.preview_sql())
    assert len(parsed) == 1
    source = parsed[0].stmt.fromClause[0]
    assert source.schemaname == relation.schema
    assert source.relname == relation.name
