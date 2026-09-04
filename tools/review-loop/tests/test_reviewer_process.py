"""Invoking the reviewer: what a run may produce, and what it may see.

These tests run real subprocesses, but only ``sys.executable`` with an inline
script. Nothing here touches the network, a reviewer API, or a credential.
"""

import os
import sys

import pytest

from review_loop.reviewer_process import (
    DEFAULT_ENV_ALLOWLIST,
    ReviewerCommandError,
    SubprocessReviewer,
    build_env,
    split_command,
)
from review_loop.verdict import VERDICT_BEGIN
from review_loop.verdict_parser import parse

from fakes import FULL_SHA, verdict_text


def _python_reviewer(script: str, **kwargs) -> SubprocessReviewer:
    return SubprocessReviewer((sys.executable, "-c", script), **kwargs)


ECHO_VERDICT = (
    "import sys\n"
    "sys.stdin.read()\n"
    f"print({verdict_text(preamble='')!r})\n"
)


def test_a_successful_reviewer_yields_its_stdout():
    run = _python_reviewer(ECHO_VERDICT).invoke("prompt")

    assert run.ok
    assert parse(run.stdout).envelope["Reviewed head SHA"] == FULL_SHA


def test_the_prompt_reaches_the_reviewer_on_stdin():
    script = "import sys; sys.stdout.write(sys.stdin.read())"

    run = _python_reviewer(script).invoke("the review prompt")

    assert run.stdout == "the review prompt"


def test_a_nonzero_exit_is_a_failure_and_yields_no_stdout():
    script = f"import sys; print({verdict_text(preamble='')!r}); sys.exit(3)"

    run = _python_reviewer(script).invoke("prompt")

    assert not run.ok
    assert "exited 3" in run.failure
    assert run.stdout == ""


def test_a_timeout_is_a_failure_rather_than_an_empty_review():
    script = "import time; time.sleep(5)"

    run = _python_reviewer(script, timeout=0.3).invoke("prompt")

    assert not run.ok
    assert "did not finish" in run.failure


def test_a_command_that_cannot_be_run_is_a_failure():
    run = SubprocessReviewer(("./definitely-not-a-real-reviewer",)).invoke("prompt")

    assert not run.ok
    assert "could not be run" in run.failure


def test_empty_stdout_is_not_a_verdict():
    run = _python_reviewer("pass").invoke("prompt")

    assert run.ok
    with pytest.raises(Exception):
        parse(run.stdout)


def test_stderr_never_becomes_the_verdict():
    """A reviewer that logs a perfect verdict to stderr has produced none."""
    script = (
        "import sys\n"
        f"sys.stderr.write({verdict_text(preamble='')!r})\n"
    )

    run = _python_reviewer(script).invoke("prompt")

    assert run.ok
    assert VERDICT_BEGIN in run.stderr
    assert run.stdout.strip() == ""


def test_unreasonably_large_output_is_refused():
    script = "print('x' * 200000)"

    run = _python_reviewer(script, max_output_bytes=1000).invoke("prompt")

    assert not run.ok
    assert "more than 1000 bytes" in run.failure


# --- command handling ------------------------------------------------------


def test_the_command_is_tokenised_and_never_interpreted_by_a_shell():
    argv = split_command("my-reviewer --flag 'two words' ; rm -rf /tmp/x")

    # One program, seven arguments. A shell would have seen two commands.
    assert argv == ("my-reviewer", "--flag", "two words", ";", "rm", "-rf", "/tmp/x")


def test_an_unterminated_quote_is_a_command_error():
    with pytest.raises(ReviewerCommandError):
        split_command("my-reviewer 'unterminated")


def test_an_empty_command_is_refused():
    with pytest.raises(ReviewerCommandError):
        split_command("   ")


def test_shell_metacharacters_do_not_start_a_second_process():
    """``;`` reaches the reviewer as an argument, not as a separator."""
    script = "import sys; print(sys.argv[1:])"
    reviewer = SubprocessReviewer((sys.executable, "-c", script, ";", "echo", "pwned"))

    run = reviewer.invoke("prompt")

    assert run.stdout.strip() == "[';', 'echo', 'pwned']"


# --- environment -----------------------------------------------------------


def test_the_reviewer_sees_an_allowlist_not_the_whole_environment():
    env = build_env({"PATH": "/bin", "SLACK_BOT_TOKEN": "secret", "HOME": "/home/x"})

    assert env == {"PATH": "/bin", "HOME": "/home/x"}
    assert "SLACK_BOT_TOKEN" not in env


def test_a_named_variable_can_be_passed_through_deliberately():
    env = build_env(
        {"PATH": "/bin", "REVIEWER_API_KEY": "k", "OTHER": "o"}, ("REVIEWER_API_KEY",)
    )

    assert env["REVIEWER_API_KEY"] == "k"
    assert "OTHER" not in env


def test_the_default_allowlist_carries_no_repository_secret():
    assert not {"SLACK_BOT_TOKEN", "HERMES_API_SERVER_KEY", "GITHUB_TOKEN", "GH_TOKEN"} & set(
        DEFAULT_ENV_ALLOWLIST
    )


def test_a_secret_in_this_process_environment_does_not_reach_the_reviewer(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-for-the-reviewer")
    script = "import os; print(os.environ.get('SLACK_BOT_TOKEN', 'absent'))"

    run = _python_reviewer(script, env=build_env(dict(os.environ))).invoke("prompt")

    assert run.stdout.strip() == "absent"
