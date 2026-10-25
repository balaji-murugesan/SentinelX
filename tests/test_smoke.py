"""Skeleton check: the four top-level packages import (D-018 layout)."""

import importlib

import pytest


@pytest.mark.parametrize("package", ["contracts", "engine", "engine.broker", "api", "cli"])
def test_package_imports(package: str) -> None:
    assert importlib.import_module(package) is not None
