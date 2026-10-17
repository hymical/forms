"""
the alembic command an operator runs to migrate, as the container image runs it
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def test_the_alembic_command_reports_each_revision_it_applies(tmp_path: Path) -> None:
    # The one-shot migrate step is otherwise silent, so an operator cannot tell
    # from its output whether it applied six revisions or none. Revision 0001 is
    # used because SQLite cannot replay the chain to head (see AGENTS.md).
    database = tmp_path / "forms.db"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"database_url=sqlite:///{database.as_posix()}",
            "upgrade",
            "0001",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Running upgrade  -> 0001" in completed.stderr
