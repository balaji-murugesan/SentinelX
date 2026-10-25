"""Database roles and migrations against a REAL throwaway Postgres (tasks 4.3 and 4.4a).

Starts ``postgres:17-alpine`` with the project's real ``infra/postgres/init.sql`` mounted
exactly as Compose does, using freshly generated random passwords (never written anywhere
permanent). Needs Docker; skipped, loudly, when Docker is unavailable.
"""

import asyncio
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command

from engine.persistence import migrate

ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = ROOT / "infra" / "postgres" / "init.sql"
MIGRATIONS = ROOT / "src" / "engine" / "persistence" / "migrations"
IMAGE = "postgres:17-alpine"
DATABASE = "sentinelx"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
]


def docker(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


@dataclass(frozen=True)
class Db:
    name: str
    port: int
    admin_password: str
    engine_password: str
    migrate_password: str

    def passwords(self) -> tuple[str, str, str]:
        return (self.admin_password, self.engine_password, self.migrate_password)

    def logs(self) -> str:
        result = docker("logs", self.name)
        return result.stdout + result.stderr

    def migrate_url(self) -> str:
        return (
            f"postgresql+asyncpg://sentinelx_migrate:{self.migrate_password}"
            f"@127.0.0.1:{self.port}/{DATABASE}"
        )


def start_container(
    tmp_path: Path, *, engine_password: str, migrate_password: str, admin_password: str
) -> str:
    """Start a Postgres container the way Compose does and return its name."""
    name = f"sentinelx-test-pg-{uuid.uuid4().hex[:8]}"
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(
        "\n".join(
            [
                f"POSTGRES_DB={DATABASE}",
                "POSTGRES_USER=postgres",
                f"POSTGRES_PASSWORD={admin_password}",
                f"ENGINE_DB_PASSWORD={engine_password}",
                f"MIGRATE_DB_PASSWORD={migrate_password}",
            ]
        )
    )
    result = docker(
        "run", "-d", "--name", name, "--env-file", str(env_file), "-p", "127.0.0.1::5432",
        "-v", f"{INIT_SQL}:/docker-entrypoint-initdb.d/10-roles.sql:ro", IMAGE,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return name


def host_port(name: str) -> int:
    fmt = '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}'
    return int(docker("inspect", "-f", fmt, name).stdout.strip())


async def _connect(db: Db, role: str, password: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        host="127.0.0.1", port=db.port, user=role, password=password, database=DATABASE
    )


def wait_until_ready(db: Db, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = docker("inspect", "-f", "{{.State.Status}}", db.name).stdout.strip()
        assert state == "running", f"postgres container {state}:\n{db.logs()[-2000:]}"
        try:
            asyncio.run(_ping(db))
            return
        except (OSError, asyncpg.PostgresError):
            time.sleep(1)
    raise AssertionError(f"postgres not ready after {timeout}s:\n{db.logs()[-2000:]}")


async def _ping(db: Db) -> None:
    conn = await _connect(db, "sentinelx_engine", db.engine_password)
    await conn.close()


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Db]:
    passwords = {k: secrets.token_hex(24) for k in ("admin", "engine", "migrate")}
    name = start_container(
        tmp_path_factory.mktemp("pg"),
        admin_password=passwords["admin"],
        engine_password=passwords["engine"],
        migrate_password=passwords["migrate"],
    )
    database = Db(
        name, host_port(name), passwords["admin"], passwords["engine"], passwords["migrate"]
    )
    try:
        wait_until_ready(database)
        yield database
    finally:
        docker("rm", "-f", name)


def run_sql(db: Db, role: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    password = db.engine_password if role == "sentinelx_engine" else db.migrate_password

    async def go() -> list[asyncpg.Record]:
        conn = await _connect(db, role, password)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def as_engine(db: Db, sql: str, *args: Any) -> list[asyncpg.Record]:
    return run_sql(db, "sentinelx_engine", sql, *args)


def as_migrate(db: Db, sql: str, *args: Any) -> list[asyncpg.Record]:
    return run_sql(db, "sentinelx_migrate", sql, *args)


# ---- roles ------------------------------------------------------------------------


def test_roles_are_plain_login_roles(db: Db) -> None:
    rows = as_migrate(
        db,
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles "
        "WHERE rolname IN ('sentinelx_engine', 'sentinelx_migrate') ORDER BY rolname",
    )
    assert [r["rolname"] for r in rows] == ["sentinelx_engine", "sentinelx_migrate"]
    for row in rows:
        assert row["rolcanlogin"]
        assert not (row["rolsuper"] or row["rolcreatedb"] or row["rolcreaterole"])


def test_wrong_password_is_rejected(db: Db) -> None:
    with pytest.raises(asyncpg.InvalidPasswordError):
        asyncio.run(_connect(db, "sentinelx_engine", "not-the-password"))
    with pytest.raises(asyncpg.InvalidPasswordError):
        asyncio.run(_connect(db, "sentinelx_engine", db.migrate_password))  # roles differ


def test_roles_use_scram_hashed_passwords_not_plaintext(db: Db) -> None:
    """Superuser view: what is stored must be a hash, never the password itself."""

    async def go() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(
            host="127.0.0.1", port=db.port, user="postgres",
            password=db.admin_password, database=DATABASE,
        )  # fmt: skip
        try:
            return await conn.fetch(
                "SELECT rolname, rolpassword FROM pg_authid WHERE rolname LIKE 'sentinelx_%'"
            )
        finally:
            await conn.close()

    rows = asyncio.run(go())
    assert len(rows) == 2
    for row in rows:
        assert row["rolpassword"].startswith("SCRAM-SHA-256$")
        assert not any(p in row["rolpassword"] for p in db.passwords())


# ---- the engine role: data only ---------------------------------------------------


def test_migrate_role_creates_table_and_engine_role_uses_it(db: Db) -> None:
    as_migrate(
        db,
        "CREATE TABLE public.probe_identity ("
        "id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, note text)",
    )
    try:
        # Insert into a table with an identity column (needs sequence access).
        inserted = as_engine(
            db, "INSERT INTO public.probe_identity(note) VALUES ('a') RETURNING id"
        )
        assert inserted[0]["id"] == 1
        as_engine(db, "UPDATE public.probe_identity SET note = 'b'")
        assert as_engine(db, "SELECT note FROM public.probe_identity")[0]["note"] == "b"
        as_engine(db, "DELETE FROM public.probe_identity")
        assert as_engine(db, "SELECT count(*) AS n FROM public.probe_identity")[0]["n"] == 0
    finally:
        as_migrate(db, "DROP TABLE public.probe_identity")


def test_default_privileges_cover_serial_sequences_too(db: Db) -> None:
    as_migrate(db, "CREATE TABLE public.probe_serial (id serial PRIMARY KEY, note text)")
    try:
        assert as_engine(db, "INSERT INTO public.probe_serial(note) VALUES ('a') RETURNING id")
        assert as_engine(db, "SELECT nextval('public.probe_serial_id_seq') AS n")[0]["n"] == 2
    finally:
        as_migrate(db, "DROP TABLE public.probe_serial")


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE public.engine_made_this (id int)",
        "CREATE SCHEMA engine_schema",
        "CREATE TEMP TABLE engine_temp (id int)",
        "CREATE ROLE engine_made_role",
        "CREATE DATABASE engine_made_db",
    ],
)
def test_engine_role_cannot_create_anything(db: Db, statement: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        as_engine(db, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE public.probe_owned ADD COLUMN x int",
        "DROP TABLE public.probe_owned",
        "TRUNCATE public.probe_owned",
        "CREATE INDEX probe_owned_idx ON public.probe_owned (note)",
    ],
)
def test_engine_role_cannot_change_existing_tables(db: Db, statement: str) -> None:
    as_migrate(db, "CREATE TABLE public.probe_owned (id int, note text)")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            as_engine(db, statement)
    finally:
        as_migrate(db, "DROP TABLE public.probe_owned")


def test_engine_role_cannot_grant_itself_or_others_more_access(db: Db) -> None:
    """GRANT by a non-owner is a warning, not an error: what matters is that nothing changes."""
    as_migrate(db, "CREATE TABLE public.probe_grant (id int)")
    try:
        as_engine(db, "GRANT ALL ON public.probe_grant TO PUBLIC")
        public_can_read = as_migrate(
            db, "SELECT has_table_privilege('public', 'public.probe_grant', 'SELECT') AS ok"
        )[0]["ok"]
        engine_can_alter = as_migrate(
            db, "SELECT pg_has_role('sentinelx_engine', 'sentinelx_migrate', 'USAGE') AS ok"
        )[0]["ok"]
        assert not public_can_read
        assert not engine_can_alter, "engine role must not inherit the migrate role"
    finally:
        as_migrate(db, "DROP TABLE public.probe_grant")


def test_engine_role_cannot_touch_migration_bookkeeping(db: Db) -> None:
    for statement in (
        "SELECT * FROM migrations.alembic_version",
        "UPDATE migrations.alembic_version SET version_num = 'x'",
        "CREATE TABLE migrations.sneaky (id int)",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            as_engine(db, statement)


# ---- migrations -------------------------------------------------------------------


def run_migrate(monkeypatch: pytest.MonkeyPatch, db: Db) -> None:
    monkeypatch.setenv("MIGRATE_DATABASE_URL", db.migrate_url())
    migrate.main()


def test_baseline_migration_creates_no_tables(
    db: Db, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_migrate(monkeypatch, db)
    run_migrate(monkeypatch, db)  # idempotent: running it again changes nothing

    version = as_migrate(db, "SELECT version_num FROM migrations.alembic_version")
    assert [r["version_num"] for r in version] == ["0001"]
    tables = as_migrate(db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert tables == [], "Stage 0 creates no application tables"
    out = capsys.readouterr().out
    assert "database migrations applied" in out
    assert not any(p in out for p in db.passwords())


def test_downgrade_to_base_works(db: Db, monkeypatch: pytest.MonkeyPatch) -> None:
    run_migrate(monkeypatch, db)
    command.downgrade(migrate.alembic_config(db.migrate_url()), "base")
    assert as_migrate(db, "SELECT version_num FROM migrations.alembic_version") == []
    run_migrate(monkeypatch, db)  # and back up again
    assert as_migrate(db, "SELECT version_num FROM migrations.alembic_version")[0][0] == "0001"


def test_failing_migration_exits_nonzero_without_leaking_credentials(
    db: Db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_migrate(monkeypatch, db)  # database at the real baseline

    scripts = tmp_path / "bad_migrations"
    (scripts / "versions").mkdir(parents=True)
    shutil.copy(MIGRATIONS / "env.py", scripts / "env.py")
    shutil.copy(MIGRATIONS / "script.py.mako", scripts / "script.py.mako")
    shutil.copy(MIGRATIONS / "versions" / "0001_baseline.py", scripts / "versions")
    (scripts / "versions" / "0002_bad.py").write_text(
        "from collections.abc import Sequence\n"
        "revision: str = '0002'\n"
        "down_revision: str | None = '0001'\n"
        "branch_labels: str | Sequence[str] | None = None\n"
        "depends_on: str | Sequence[str] | None = None\n"
        "def upgrade() -> None:\n"
        "    raise RuntimeError('boom password=hunter2 at postgresql://u:hunter2pw@host/db')\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )
    real_config = migrate.alembic_config

    def bad_config(url: str) -> Any:
        config = real_config(url)
        config.set_main_option("script_location", str(scripts))
        return config

    monkeypatch.setattr(migrate, "alembic_config", bad_config)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        run_migrate(monkeypatch, db)

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "database migration failed" in out
    for leaked in ("hunter2", "hunter2pw", *db.passwords()):
        assert leaked not in out
    # The failed upgrade rolled back: still at the baseline.
    assert as_migrate(db, "SELECT version_num FROM migrations.alembic_version")[0][0] == "0001"


def test_missing_migrate_url_stops_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        migrate.main()
    assert "MIGRATE_DATABASE_URL" in str(exit_info.value)


# ---- credentials never reach logs -------------------------------------------------


def test_postgres_logs_contain_no_passwords(db: Db) -> None:
    logs = db.logs()
    assert "database system is ready to accept connections" in logs
    assert not any(p in logs for p in db.passwords())


# ---- init.sql refuses weak or missing passwords -----------------------------------


def good() -> str:
    return secrets.token_hex(24)


@pytest.mark.parametrize(
    ("engine_pw", "migrate_pw", "expected"),
    [
        ("short", good(), "ENGINE_DB_PASSWORD must be set and at least 16 characters"),
        ("", good(), "ENGINE_DB_PASSWORD must be set and at least 16 characters"),
        (good(), "short", "MIGRATE_DB_PASSWORD must be set and at least 16 characters"),
        ("a" * 24, "a" * 24, "must differ"),
    ],
    ids=["engine-short", "engine-empty", "migrate-short", "identical"],
)
def test_init_refuses_bad_passwords(
    tmp_path: Path, engine_pw: str, migrate_pw: str, expected: str
) -> None:
    name = start_container(
        tmp_path, engine_password=engine_pw, migrate_password=migrate_pw, admin_password=good()
    )
    try:
        deadline = time.monotonic() + 90
        state = ""
        while time.monotonic() < deadline:
            state = docker("inspect", "-f", "{{.State.Status}}", name).stdout.strip()
            if state == "exited":
                break
            time.sleep(1)
        logs = docker("logs", name)
        text = logs.stdout + logs.stderr
        assert state == "exited", f"init did not abort:\n{text[-1500:]}"
        assert expected in text
    finally:
        docker("rm", "-f", name)
