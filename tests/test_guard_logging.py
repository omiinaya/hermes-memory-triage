"""A guard refusal must reach the decision log, not just `errors`.

`self.errors` is what the summary reports, but in auto mode the summary is
read by nobody: the plugin's own logs need someone tailing them, and an
unattended run fires from a session hook. Before `add_error`, a run whose
plan was entirely refused produced a decisions.jsonl full of `applied`
lines and nothing else, which reads exactly like a clean success -- the
most dangerous possible shape for an audit trail.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memtriage import config as config_mod  # noqa: E402
from memtriage import executor as executor_mod  # noqa: E402
from memtriage import store as store_mod  # noqa: E402

RUN = "20260927-TEST-LOG"


def _entry(i: int) -> str:
    return f"durable knowledge about topic {i}. unique clause {i}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import os

    hermes = pathlib.Path(os.environ["HERMES_HOME"])
    data = pathlib.Path(os.environ["MEMTRIAGE_HOME"])
    skill = hermes / "skills" / "creative" / "house-rules" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# House rules\n\nCurated doctrine.\n", encoding="utf-8")
    c = config_mod.Config.load()
    c.data_dir = data
    return {"cfg": c, "data": data}


def _log_lines(data) -> list:
    f = data / "decisions.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def test_a_refusal_reaches_the_log(env):
    store_mod.write_entries("memory", [_entry(i) for i in range(10)])
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(
        [
            {"action": "route-to-skill", "target": "memory", "index": i,
             "skill_name": "house-rules", "text": _entry(i)}
            for i in range(10)
        ],
        RUN, "test",
    )
    blocked = [d for d in _log_lines(env["data"]) if d.get("level") == "blocked"]
    assert blocked, "the floor's refusal never reached decisions.jsonl"
    joined = " ".join(d["msg"] for d in blocked)
    assert "emptied entirely" in joined, (
        f"a refusal was logged but not THIS one: {joined!r}"
    )


def test_the_log_goes_to_cfg_data_dir_not_the_env_default(env, monkeypatch):
    """A sandbox run must not create a log in the real data dir."""
    store_mod.write_entries("memory", [_entry(i) for i in range(10)])
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(
        [
            {"action": "route-to-skill", "target": "memory", "index": i,
             "skill_name": "house-rules", "text": _entry(i)}
            for i in range(10)
        ],
        RUN, "test",
    )
    assert _log_lines(env["data"]), "nothing was written to cfg.data_dir"
    live = pathlib.Path("/root/.memtriage/decisions.jsonl")
    if live.exists():
        assert not any(
            d.get("run_id") == RUN for d in
            [json.loads(l) for l in live.read_text().splitlines() if l.strip()]
        ), "the sandbox run leaked into the LIVE decision log"


def test_add_error_keeps_the_errors_contract(env):
    """add_error must still populate self.errors -- callers read that list."""
    ex = executor_mod.Executor(env["cfg"])
    ex._run_id = RUN
    ex.add_error("something was refused")
    assert "something was refused" in ex.errors
    assert any(
        d.get("level") == "blocked" and "something was refused" in d["msg"]
        for d in _log_lines(env["data"])
    )
