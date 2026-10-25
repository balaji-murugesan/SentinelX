"""Static checks on docker-compose.yml, the Dockerfiles, init.sql and .env.example.

No Docker needed. They pin down the stack's rules (service-stack spec, tasks 4.2-4.4) and the
"no credentials in files" requirement, so a careless edit fails CI.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
LONG_RUNNING = {"postgres", "redis", "otel-collector", "engine", "api", "caddy"}
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY", re.IGNORECASE)
# ${VAR:?message}: required variable, no default value
REQUIRED_REF = re.compile(r"\$\{[A-Z_]+:\?[^}]+\}")
# Secrets that may legitimately be empty (DHAN_ACCESS_TOKEN with FEED_SOURCE=simulated). They
# may only use an EMPTY default, never a literal; the engine's settings still require the
# token whenever FEED_SOURCE=dhan. Owner-approved allow-list (add-market-data).
OPTIONAL_SECRETS = frozenset({"DHAN_ACCESS_TOKEN"})
OPTIONAL_REF = re.compile(r"\$\{[A-Z_]+:-\}")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(compose["services"])


def env_of(service: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in (service.get("environment") or {}).items()}


# ---- topology (service-stack spec) ------------------------------------------------


def test_expected_services(services: dict[str, dict[str, Any]]) -> None:
    assert set(services) == LONG_RUNNING | {"migrate"}


def test_long_running_services_restart_and_have_healthchecks(
    services: dict[str, dict[str, Any]],
) -> None:
    for name in LONG_RUNNING:
        assert services[name]["restart"] == "unless-stopped", name
        assert "healthcheck" in services[name], name


def test_migrate_is_one_shot(services: dict[str, dict[str, Any]]) -> None:
    migrate = services["migrate"]
    assert migrate["restart"] == "no"
    assert "healthcheck" not in migrate
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_engine_waits_for_dependencies_and_migration(
    services: dict[str, dict[str, Any]],
) -> None:
    deps = services["engine"]["depends_on"]
    assert deps["migrate"]["condition"] == "service_completed_successfully"
    assert deps["postgres"]["condition"] == "service_healthy"
    assert deps["redis"]["condition"] == "service_healthy"


def test_engine_does_not_depend_on_api(services: dict[str, dict[str, Any]]) -> None:
    assert "api" not in services["engine"]["depends_on"]


def test_data_services_publish_no_ports(services: dict[str, dict[str, Any]]) -> None:
    for name in ("postgres", "redis", "engine", "api", "migrate", "otel-collector"):
        assert "ports" not in services[name], f"{name} must not publish host ports"


def test_caddy_listens_on_localhost_only(services: dict[str, dict[str, Any]]) -> None:
    ports = services["caddy"]["ports"]
    assert ports
    assert all(str(p).startswith("127.0.0.1:") for p in ports), ports


def test_caddyfile_has_stage_7_tls_todo() -> None:
    text = (ROOT / "infra/caddy/Caddyfile").read_text(encoding="utf-8")
    assert "TODO(Stage 7)" in text
    assert "Tailscale" in text


def test_postgres_healthcheck_uses_tcp(services: dict[str, dict[str, Any]]) -> None:
    """A unix-socket check goes healthy during first-time init, before the roles exist."""
    test = " ".join(services["postgres"]["healthcheck"]["test"])
    assert "pg_isready" in test
    assert "-h 127.0.0.1" in test


# ---- paper by default -------------------------------------------------------------


@pytest.mark.parametrize("name", ["engine", "api"])
def test_compose_defaults_to_paper_and_never_confirms_live(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    env = env_of(services[name])
    assert env["TRADING_MODE"] == "${TRADING_MODE:-paper}"
    assert env["LIVE_CONFIRM"] == "${LIVE_CONFIRM:-}"


# ---- each service sees only what it needs -----------------------------------------


def test_no_env_file_hands_everything_to_every_service(
    services: dict[str, dict[str, Any]],
) -> None:
    assert not [name for name, s in services.items() if "env_file" in s]


def test_api_has_no_database_settings(services: dict[str, dict[str, Any]]) -> None:
    env = env_of(services["api"])
    assert not [k for k in env if re.search(r"DATABASE|DB_|POSTGRES|MIGRATE", k)], env
    assert "postgres" not in services["api"]["depends_on"]


def test_engine_gets_engine_role_only(services: dict[str, dict[str, Any]]) -> None:
    env = env_of(services["engine"])
    assert "sentinelx_engine" in env["DATABASE_URL"]
    assert not [k for k in env if "MIGRATE" in k or k == "POSTGRES_PASSWORD"], env
    assert "sentinelx_migrate" not in str(env)


def test_migrate_gets_migrate_role_only(services: dict[str, dict[str, Any]]) -> None:
    env = env_of(services["migrate"])
    assert set(env) == {"MIGRATE_DATABASE_URL", "LOG_LEVEL"}
    assert "sentinelx_migrate" in env["MIGRATE_DATABASE_URL"]
    assert "sentinelx_engine" not in str(env)


# ---- no credentials in files ------------------------------------------------------


def test_compose_secrets_are_required_references_not_literals(
    services: dict[str, dict[str, Any]],
) -> None:
    for name, service in services.items():
        for key, value in env_of(service).items():
            if key in OPTIONAL_SECRETS:
                assert OPTIONAL_REF.fullmatch(value), (
                    f"{name}.{key} must be a ${{VAR:-}} reference with an empty default"
                )
            elif SECRET_NAME.search(key):
                assert REQUIRED_REF.fullmatch(value), (
                    f"{name}.{key} must be a ${{VAR:?...}} reference"
                )


def test_compose_urls_take_their_password_from_a_reference(
    services: dict[str, dict[str, Any]],
) -> None:
    for name, service in services.items():
        for key, value in env_of(service).items():
            if re.search(r"://[^/@\s]*@", value):  # a URL with credentials in it
                assert re.search(r"://[^:/@]+:\$\{[A-Z_]+:\?[^}]+\}@", value), (
                    f"{name}.{key} must build its password from a ${{VAR:?...}} reference"
                )


def test_compose_text_has_no_literal_password_values() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not SECRET_NAME.search(stripped.split(":", 1)[0]):
            continue
        value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        assert value.startswith("${") or value == "", f"literal secret value in: {stripped!r}"


def test_init_sql_has_no_passwords() -> None:
    sql = (ROOT / "infra/postgres/init.sql").read_text(encoding="utf-8")
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "\\getenv engine_password ENGINE_DB_PASSWORD" in code
    assert "\\getenv migrate_password MIGRATE_DB_PASSWORD" in code
    # every PASSWORD keyword is followed by a psql variable, never a quoted literal
    passwords = re.findall(r"\bLOGIN PASSWORD\s+(\S+)", code)
    assert len(passwords) == 2  # one per role
    assert all(p.startswith(":'") for p in passwords), passwords
    assert not re.search(r"\bPASSWORD\s+'", code)


def test_init_sql_grants_cover_tables_and_sequences() -> None:
    sql = (ROOT / "infra/postgres/init.sql").read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", sql)
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE sentinelx_migrate IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sentinelx_engine"
    ) in flat
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE sentinelx_migrate IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO sentinelx_engine"
    ) in flat


@pytest.mark.parametrize("dockerfile", ["engine", "api", "otel-collector"])
def test_dockerfiles_contain_no_credentials(dockerfile: str) -> None:
    text = (ROOT / f"docker/{dockerfile}.Dockerfile").read_text(encoding="utf-8")
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        assert not (re.match(r"\s*(ENV|ARG)\b", code) and SECRET_NAME.search(code)), line
        assert not re.search(r"://[^/@\s]+:[^/@\s$]+@", code), f"URL with credentials: {line}"
    assert "COPY .env" not in text


@pytest.mark.parametrize("dockerfile", ["engine", "api"])
def test_app_images_run_as_non_root_on_python_312(dockerfile: str) -> None:
    text = (ROOT / f"docker/{dockerfile}.Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in text
    assert re.search(r"^USER sentinelx$", text, re.MULTILINE)


def test_api_image_has_no_migrations() -> None:
    text = (ROOT / "docker/api.Dockerfile").read_text(encoding="utf-8")
    assert "alembic" not in text.lower()


def test_dockerignore_keeps_env_files_out_of_images() -> None:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines
    assert ".env.*" in lines


def test_gitignore_keeps_env_files_out_of_git() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines
    assert "!.env.example" in lines


# ---- .env.example -----------------------------------------------------------------


def example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def test_env_example_holds_placeholders_only() -> None:
    """Password variables are empty; URLs carry the visible placeholder 'change-me'."""
    for name, value in example_values().items():
        if name.endswith("_PASSWORD"):
            assert value == "", f"{name} must be empty in .env.example"
        if "://" in value and "@" in value:
            assert ":change-me@" in value, f"{name} must use the 'change-me' placeholder"


def test_env_example_documents_every_compose_secret() -> None:
    names = example_values()
    for required in ("POSTGRES_PASSWORD", "ENGINE_DB_PASSWORD", "MIGRATE_DB_PASSWORD"):
        assert required in names
        assert names[required] == ""
