"""pytest 公共夹具：每个用例使用独立 SQLite 文件重建应用。"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    import app.config as config
    importlib.reload(config)

    import app.main as main
    importlib.reload(main)

    with TestClient(main.app) as c:
        yield c
