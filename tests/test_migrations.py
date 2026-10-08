"""Alembic 0002 is additive: it upgrades a populated 0001 database without touching existing rows
and produces the same schema the ORM models expect."""
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.db import Base


def _cfg():
    return Config("alembic.ini")


def test_upgrade_keeps_existing_data_and_matches_models(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_cfg(), "0001")
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO companies (canonical_name, suggestion_attempts) VALUES ('nvidia', 0)"))
        c.execute(sa.text("INSERT INTO scores (company_id, category, score) VALUES (1, 'energy_kardashev', 4.2)"))
    command.upgrade(_cfg(), "head")
    insp = sa.inspect(eng)
    for table in Base.metadata.sorted_tables:
        cols = {c["name"] for c in insp.get_columns(table.name)}
        assert cols == {c.name for c in table.columns}, table.name
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT canonical_name, current_run_id FROM companies")).one() == ("nvidia", None)
        assert c.execute(sa.text("SELECT score FROM scores")).scalar() == 4.2
    idx = {i["name"]: i for i in insp.get_indexes("judgment_runs")}
    assert idx["uq_judgment_runs_active_company"]["unique"]
    command.downgrade(_cfg(), "0001")
    assert "judgment_runs" not in sa.inspect(eng).get_table_names()
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM scores")).scalar() == 1
