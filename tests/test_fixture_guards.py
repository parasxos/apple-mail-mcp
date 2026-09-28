import os
from pathlib import Path

from email_mcp import config, doctor, plan


def test_monkeypatch_undo_preserves_host_guards(
    monkeypatch, tmp_path, home_guard, state_dir_guard,
):
    launchctl = plan._launchctl
    agent_loaded = doctor._agent_loaded
    agent_last_exit = doctor._agent_last_exit
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    monkeypatch.delenv("EMAIL_MCP_STATE_DIR")
    monkeypatch.setattr(plan, "_launchctl", lambda *args: "override")
    monkeypatch.setattr(doctor, "_agent_loaded", lambda label: True)
    monkeypatch.setattr(doctor, "_agent_last_exit", lambda label: 17)
    assert plan._launchctl("print", "fixture-agent") == "override"
    assert doctor._agent_loaded("fixture-agent") is True
    assert doctor._agent_last_exit("fixture-agent") == 17

    monkeypatch.undo()

    assert os.environ["HOME"] == str(home_guard)
    assert Path.home() == home_guard
    assert os.environ["EMAIL_MCP_STATE_DIR"] == str(state_dir_guard)
    assert config.state_dir() == state_dir_guard
    assert plan._launchctl is launchctl
    assert doctor._agent_loaded is agent_loaded
    assert doctor._agent_last_exit is agent_last_exit
    result = plan._launchctl("print", "fixture-agent")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert doctor._agent_loaded("fixture-agent") is None
    assert doctor._agent_last_exit("fixture-agent") is None
