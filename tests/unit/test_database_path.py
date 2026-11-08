from pathlib import Path

import pytest

from axile.common.config import settings
from axile.server.core.db import database_path


def test_relative_database_path_survives_channel_cwd_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(settings, "sqlalchemy_database_uri", "sqlite+aiosqlite:///./data/axile.db")
    expected = Path.cwd() / "data" / "axile.db"

    monkeypatch.chdir(tmp_path)

    assert Path(database_path()) == expected
