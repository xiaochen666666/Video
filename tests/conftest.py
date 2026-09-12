import pytest
from backend import store


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    with store.connect() as db:
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, source TEXT, title TEXT, status TEXT, stage TEXT, error TEXT, created TEXT, result TEXT)"
        )
    return tmp_path
