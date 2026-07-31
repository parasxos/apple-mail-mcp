"""`Answers.from_dict` validation (email_mcp.lifecycle, setup half).

A scripted answers file is the only way to arm the dangerous setup
switches non-interactively, so its parse is the fence. The enum fields
and the identities list were already validated; the BOOLEANS were not,
and every non-empty string is truthy in Python — `"skip_permissions":
"false"` read as "skip every red permission check", which is the exact
opposite of what the file says. These tests pin the rejection.
"""
from __future__ import annotations

import json

import pytest

from email_mcp import lifecycle

BOOL_FIELDS = ("read_only", "skip_permissions", "install_dispatcher",
               "install_fts_agent", "self_send")


def test_a_stringly_typed_false_would_have_been_truthy():
    """The premise, stated once so the rejections below have a reason:
    the value these tests reject evaluates TRUE."""
    assert bool("false") is True


@pytest.mark.parametrize("name", BOOL_FIELDS)
@pytest.mark.parametrize("value", ["false", "true", "no", 0, 1, None, []])
def test_non_bool_flag_values_are_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        lifecycle.Answers.from_dict({name: value})


@pytest.mark.parametrize("name", BOOL_FIELDS)
def test_real_bools_are_accepted_both_ways(name):
    for value in (True, False):
        answers = lifecycle.Answers.from_dict({name: value})
        assert getattr(answers, name) is value


def test_valid_full_answers_object_still_parses():
    """The fence must not narrow what a legitimate file may say."""
    answers = lifecycle.Answers.from_dict({
        "skip_permissions": True,
        "install_dispatcher": True,
        "identity_action": "add",
        "identities": [{"name": "work", "from_addr": "a@example.org"}],
        "default_identity": "work",
        "fts_choice": "quick",
        "fts_limit": 200,
    })
    assert answers.skip_permissions is True
    assert answers.identities[0]["name"] == "work"


def test_setup_cli_rejects_a_stringly_typed_flag_file(tmp_path, capsys):
    """End to end: `setup --answers FILE` exits 2 with a usage error
    rather than running with red permission checks silently skipped."""
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"skip_permissions": "false"}))

    assert lifecycle.run_setup_cli(["--answers", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skip_permissions" in captured.err
