import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_repo"


@pytest.fixture
def broken_repo(tmp_path, monkeypatch):
    """A fresh broken copy of sample_repo plus an isolated sandbox root."""
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    # reset.sh is the canonical broken state
    import subprocess
    subprocess.run(["bash", str(repo / "reset.sh")], check=True, capture_output=True)
    monkeypatch.setenv("FIXIT_SANDBOX_ROOT", str(tmp_path / "sandbox"))
    return repo
