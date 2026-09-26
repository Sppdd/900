import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep tests away from the developer's installed skills and runs."""
    monkeypatch.setenv("SANDCODER_HOME", str(tmp_path / "sandcoder-home"))
    monkeypatch.delenv("SANDCODER_SKILLS_PATH", raising=False)
