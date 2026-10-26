"""Migration 0009: additive paid-API tables; upgrade/downgrade on SQLite."""
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.db import Base


def _cfg():
    return Config("alembic.ini")


def test_0009_roundtrip(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/paid.db"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_cfg(), "0008")
    eng = sa.create_engine(url)
    assert "api_customers" not in sa.inspect(eng).get_table_names()
    command.upgrade(_cfg(), "0009")
    tables = set(sa.inspect(eng).get_table_names())
    assert {"api_customers", "api_entitlements", "api_keys", "stripe_events"} <= tables
    # ORM columns match
    insp = sa.inspect(eng)
    for name in ("api_customers", "api_entitlements", "api_keys", "stripe_events"):
        table = Base.metadata.tables[name]
        cols = {c["name"] for c in insp.get_columns(name)}
        assert cols == {c.name for c in table.columns}, name
    command.downgrade(_cfg(), "0008")
    tables = set(sa.inspect(eng).get_table_names())
    assert "api_customers" not in tables
    assert "page_views" in tables  # 0008 intact
