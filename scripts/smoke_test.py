"""Smoke test for a running SentinelX stack (Stage 0 acceptance, PRD section 11).

Run it after ``docker compose up -d --build --wait``. Works the same from PowerShell and Bash:

    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --project-name sentinelx-ci --base-url http://127.0.0.1:8081

Checks:
  1. the six long-running services are healthy and ``migrate`` exited 0
  2. engine and api announced PAPER mode (and nothing announced LIVE)
  3. POST /api/diagnostics/ping through Caddy returns ok
  4. that ping appears in the collector as ONE trace with spans from both api and engine
  5. the engine's database role cannot create a table, but can query
  6. stopping the api leaves the engine healthy (the api is started again afterwards)

It never reads secrets: the database check runs inside the postgres container, using the
password already in that container's environment. Exit status 0 means every check passed.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from typing import Any

LONG_RUNNING = ("postgres", "redis", "otel-collector", "engine", "api", "caddy")


class Stack:
    """Thin wrapper over ``docker compose`` for one project."""

    def __init__(self, project_name: str | None, env_file: str | None) -> None:
        self.base = ["docker", "compose"]
        if project_name:
            self.base += ["--project-name", project_name]
        if env_file:
            self.base += ["--env-file", env_file]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.base, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def services(self) -> dict[str, dict[str, Any]]:
        out = self.run("ps", "-a", "--format", "json").stdout.strip()
        if not out:
            return {}
        # Compose prints either one JSON array or one JSON object per line, by version.
        rows = json.loads(out) if out.startswith("[") else [json.loads(x) for x in out.splitlines()]
        return {row["Service"]: row for row in rows}

    def logs(self, service: str) -> str:
        return self.run("logs", "--no-log-prefix", "--no-color", service).stdout


def wait_for(condition: Callable[[], bool], timeout: float, interval: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def traces(collector_log: str) -> dict[str, set[tuple[str, str]]]:
    """Trace ID -> {(service, span name)} parsed from the collector's debug exporter output."""
    found: dict[str, set[tuple[str, str]]] = {}
    service = trace_id = ""
    for line in collector_log.splitlines():
        if m := re.search(r"service\.name: Str\(([\w-]+)\)", line):
            service = m.group(1)
        elif m := re.search(r"Trace ID\s*:\s*(\w+)", line):
            trace_id = m.group(1)
        elif (m := re.search(r"^\s*Name\s*:\s*(.+?)\s*$", line)) and trace_id:
            found.setdefault(trace_id, set()).add((service, m.group(1)))
    return found


# ---- the checks ---------------------------------------------------------------------


def check_services(stack: Stack) -> str:
    def all_healthy() -> bool:
        rows = stack.services()
        return all(rows.get(s, {}).get("Health") == "healthy" for s in LONG_RUNNING)

    if not wait_for(all_healthy, timeout=120):
        rows = stack.services()
        bad = {s: (rows.get(s) or {}).get("Health") or "missing" for s in LONG_RUNNING}
        raise AssertionError(f"not healthy: { {k: v for k, v in bad.items() if v != 'healthy'} }")
    migrate = stack.services().get("migrate", {})
    if migrate.get("State") != "exited" or int(migrate.get("ExitCode", -1)) != 0:
        raise AssertionError(
            f"migrate: state={migrate.get('State')} exit={migrate.get('ExitCode')}"
        )
    return "6 services healthy, migrate exited 0"


def check_paper_mode(stack: Stack) -> str:
    for service in ("engine", "api"):
        modes = {
            record.get("mode")
            for line in stack.logs(service).splitlines()
            if line.startswith("{") and (record := json.loads(line)).get("mode")
        }
        if modes != {"paper"}:
            raise AssertionError(f"{service} announced modes {modes or 'none'}, expected paper")
    return "engine and api announced PAPER"


def check_ping_and_trace(stack: Stack, base_url: str) -> str:
    before = set(traces(stack.logs("otel-collector")))
    request = urllib.request.Request(f"{base_url}/api/diagnostics/ping", data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (fixed local URL)
        body = json.loads(response.read())
    if body.get("ok") is not True:
        raise AssertionError(f"ping returned {body}")

    def joined_trace() -> str | None:
        for trace_id, spans in traces(stack.logs("otel-collector")).items():
            if trace_id in before:
                continue
            if {("api", "diagnostics.ping"), ("engine", "diagnostics.answer_ping")} <= spans:
                return trace_id
        return None

    if not wait_for(lambda: joined_trace() is not None, timeout=30):
        raise AssertionError("no new trace containing both api and engine spans")
    return f"ping ok in {body['rtt_ms']:.1f} ms; trace {joined_trace()} spans api + engine"


def engine_sql(stack: Stack, sql: str) -> subprocess.CompletedProcess[str]:
    # Single quotes: $ENGINE_DB_PASSWORD expands inside the container, never on this machine.
    script = (
        'PGPASSWORD="$ENGINE_DB_PASSWORD" psql -h 127.0.0.1 -U sentinelx_engine '
        f'-d sentinelx -v ON_ERROR_STOP=1 -tAc "{sql}"'
    )
    return stack.run("exec", "-T", "postgres", "sh", "-c", script)


def check_engine_role(stack: Stack) -> str:
    ok = engine_sql(stack, "SELECT 1")
    if ok.returncode != 0 or ok.stdout.strip() != "1":
        raise AssertionError(f"engine role cannot query: {ok.stderr.strip()}")
    denied = engine_sql(stack, "CREATE TABLE smoke_should_fail (id int)")
    if denied.returncode == 0 or "permission denied" not in denied.stderr:
        raise AssertionError("engine role was able to create a table")
    # The trading tables (migration 0002) are readable and writable by the engine role: the
    # insert runs inside a transaction that is rolled back, so nothing is left behind.
    written = engine_sql(
        stack,
        "BEGIN; INSERT INTO command_results VALUES ('smoke', 'smoke', 'smoke', false, NULL, "
        "NULL, now()); SELECT count(*) FROM orders; ROLLBACK;",
    )
    if written.returncode != 0:
        raise AssertionError(f"engine role cannot use the trading tables: {written.stderr.strip()}")
    return "engine role can query and write trading tables, cannot create tables"


def check_engine_without_api(stack: Stack) -> str:
    stack.run("stop", "api")
    try:
        time.sleep(12)  # longer than one engine health-check interval
        engine = stack.services().get("engine", {})
        if engine.get("State") != "running" or engine.get("Health") != "healthy":
            raise AssertionError(f"engine became {engine.get('State')}/{engine.get('Health')}")
    finally:
        stack.run("start", "api")
    if not wait_for(lambda: stack.services().get("api", {}).get("Health") == "healthy", 60):
        raise AssertionError("api did not become healthy again after restart")
    return "engine stayed healthy with the api stopped; api restarted"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-name", help="compose project name (default: from compose file)")
    parser.add_argument("--env-file", help="compose env file (default: .env)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Caddy URL")
    parser.add_argument(
        "--skip-disruptive", action="store_true", help="do not stop and restart the api"
    )
    args = parser.parse_args()
    stack = Stack(args.project_name, args.env_file)

    checks: list[tuple[str, Callable[[], str]]] = [
        ("services", lambda: check_services(stack)),
        ("paper mode", lambda: check_paper_mode(stack)),
        ("ping + trace", lambda: check_ping_and_trace(stack, args.base_url)),
        ("db roles", lambda: check_engine_role(stack)),
    ]
    if not args.skip_disruptive:
        checks.append(("engine without api", lambda: check_engine_without_api(stack)))

    failures = 0
    for name, check in checks:
        try:
            print(f"PASS  {name:<20} {check()}")
        except Exception as exc:  # report every check, then fail overall
            failures += 1
            print(f"FAIL  {name:<20} {exc}")
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
