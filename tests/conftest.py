"""Loads 2_Postgres_Join.py, which cannot be imported by name because it starts with a digit.

Every test file takes the module from the `join` fixture so there is one loader, and one place to
change if the script is ever renamed.
"""

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "2_Postgres_Join.py"


def loadJoin():
    spec = importlib.util.spec_from_file_location("join", MODULE_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def join():
    return loadJoin()
