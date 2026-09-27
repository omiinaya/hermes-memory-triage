"""Shared pytest fixtures for hermes-memory-triage.

ISOLATION IS MANDATORY HERE. The plugin resolves three roots from the
environment, and a test that does not redirect all of them will mutate the
live install:

* ``HERMES_HOME``    -> the memory stores AND the skills tree that
                        route-to-skill writes SKILL.md files into.
* ``MEMTRIAGE_HOME`` -> config, ledger, quarantine, reports, state.
* ``scripts_dir``    -> route-to-script writes real executables.

Before this fixture existed, ``tests/test_wave2.py`` created
``~/.hermes/skills/tools/house-conventions/SKILL.md`` on the real
filesystem during a unit run. The autouse fixture below makes that
impossible rather than relying on every test remembering to do it.
"""

import os

import pytest

STORE_FILES = ("MEMORY.md", "USER.md")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Redirect every path root the plugin can write to, for every test."""
    hermes = tmp_path / "hermes"
    data = tmp_path / "data"
    scripts = tmp_path / "scripts"
    for d in (hermes, data, scripts):
        d.mkdir(parents=True, exist_ok=True)
    (hermes / "memories").mkdir(parents=True, exist_ok=True)
    (hermes / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CERVEAU_MAX_PAYLOAD", "2000")
    # A test that reaches the REAL gateway writes into production memory --
    # the exact failure the 2026-09-27 pre-enablement audit found (a "fully
    # isolated" dry run pushed three real entries). Redirect the provider at
    # a closed port for the whole session so a missed guard is a loud
    # connection error, not a silent production write. A test that genuinely
    # needs the wire must opt in per-test via its own monkeypatch.
    monkeypatch.setenv("TDAI_LLM_API_KEY", "")
    monkeypatch.setenv("MEMORY_TENCENTDB_LLM_API_KEY", "")
    yield tmp_path


@pytest.fixture()
def monkeypatch_env(monkeypatch, tmp_path):
    """Backwards-compatible alias; isolated_env already covers this."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    yield tmp_path


@pytest.fixture()
def live_hermes_home():
    """The REAL hermes home, for tests that must assert the fixture applies.

    Used as a guard test: if this ever equals the isolated home, the
    isolation is broken and every other test is untrustworthy.
    """
    return os.path.expanduser("~/.hermes")
