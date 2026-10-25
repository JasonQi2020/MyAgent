import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

import seed_db  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Skip integration tests in a full run; run them when asked for.

    They run with `-m integration`, or when a test file or test is named directly
    (e.g. `pytest tests/test_feishu_credential.py`, or the run button in PyCharm).
    """
    if config.option.markexpr or any(arg.split("::")[0].endswith(".py") for arg in config.args):
        return
    skip = pytest.mark.skip(reason="integration test: run with -m integration, or run its file directly")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def demo_db(tmp_path: Path) -> Path:
    path = tmp_path / "demo.db"
    seed_db.build(path, days=20)
    return path


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Tests never touch the real state/ folder (tasks, the owner's chat, groups)."""
    import agent_demo.tasks as tasks

    monkeypatch.setattr(tasks, "STATE_DIR", tmp_path / "state")
