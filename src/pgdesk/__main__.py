"""Launch PGDesk with local connection references and an interactive terminal."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pgdesk.app import PgDesk
from pgdesk.config import CONFIG_DIR, load_config


def main() -> None:
    """Parse only the config location and run the real Textual interface."""
    parser = argparse.ArgumentParser(
        description="Keyboard-first PostgreSQL workspaces. Ctrl+N connects; F1 shows help."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_DIR / "config.toml",
        help="Cluster TOML configuration (default: ~/.config/pgdesk/config.toml)",
    )
    args = parser.parse_args()
    # Pool warnings include raw endpoints. Health is exposed through sanitized UI status.
    logging.getLogger("psycopg.pool").disabled = True
    try:
        config = load_config(args.config)
        app = PgDesk(config, args.config)
    except (ValueError, TypeError, OSError) as error:
        parser.exit(
            2,
            f"Invalid PGDesk configuration ({type(error).__name__}); check config and settings files.\n",
        )
    app.run()


if __name__ == "__main__":
    main()
