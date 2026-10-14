import copy
import os
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def daniel(tmp_path, monkeypatch):
    """Import daniel.py fresh with HOME and the config path pointed at a temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("XAI_API_KEY", "GEMINI_API_KEY", "FAKE_SECRET_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    spec = importlib.util.spec_from_file_location("daniel_under_test", Path(os.environ.get("DANIEL_PY", ROOT / "daniel.py")))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daniel_under_test"] = mod
    spec.loader.exec_module(mod)
    mod.CONFIG_DIR = tmp_path / ".config" / "agent-orchestrator"
    mod.CONFIG_PATH = mod.CONFIG_DIR / "config.json"
    return mod


@pytest.fixture()
def cfg(daniel, tmp_path):
    c = copy.deepcopy(daniel.DEFAULTS)
    c["tasks_root"] = str(tmp_path / "tasks")
    c["allowed_dirs"] = [str(tmp_path)]
    return c
