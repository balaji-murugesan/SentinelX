"""Proves each import-linter contract can actually fail (task 1.4).

A contract that is never seen failing could be silently misconfigured (e.g. a typo in a
module name) and still report KEPT. So each test copies the real src/ tree + config into a
temp directory, adds one violating module, and expects `lint-imports` to fail.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Call import-linter's entry point in a subprocess whose cwd/PYTHONPATH is the temp tree.
_RUN_LINTER = (
    "from importlinter.cli import lint_imports; "
    "raise SystemExit(lint_imports(config_filename='pyproject.toml'))"
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Copy of src/ and pyproject.toml that a test may add violating modules to."""
    shutil.copytree(
        REPO_ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def run_linter(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _RUN_LINTER],
        cwd=root,
        env={"PYTHONPATH": str(root / "src"), "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )


def add_module(root: Path, relative: str, source: str) -> None:
    path = root / "src" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_clean_tree_passes(sandbox: Path) -> None:
    result = run_linter(sandbox)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("module", "source", "contract"),
    [
        ("api/bad.py", "import engine\n", "api must not import engine"),
        ("api/bad_sub.py", "from engine import broker\n", "api must not import engine"),
        (
            "contracts/bad.py",
            "import engine\n",
            "contracts must not import other internal packages",
        ),
        (
            "contracts/bad_api.py",
            "import api\n",
            "contracts must not import other internal packages",
        ),
        (
            "contracts/bad_cli.py",
            "import cli\n",
            "contracts must not import other internal packages",
        ),
        ("engine/bad.py", "import dhanhq\n", "only engine.broker may import dhanhq"),
        ("api/bad_dhan.py", "import dhanhq\n", "only engine.broker may import dhanhq"),
    ],
)
def test_violation_is_rejected(sandbox: Path, module: str, source: str, contract: str) -> None:
    add_module(sandbox, module, source)
    result = run_linter(sandbox)
    assert result.returncode != 0, result.stdout
    assert f"{contract} BROKEN" in result.stdout


def test_engine_broker_may_import_dhanhq(sandbox: Path) -> None:
    add_module(sandbox, "engine/broker/adapter.py", "import dhanhq\n")
    result = run_linter(sandbox)
    assert result.returncode == 0, result.stdout
