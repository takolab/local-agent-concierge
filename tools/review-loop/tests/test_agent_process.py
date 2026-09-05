"""Invoking the coding agent: what a run may produce, and what it may see.

These tests run real subprocesses, but only ``sys.executable`` with an inline
script. Nothing here touches the network, an agent API, or a credential.

They are deliberately a near-mirror of ``test_reviewer_process.py``: the two
roles share one mechanism, and the point of these tests is that the shared
mechanism keeps every property for the *writable* role too. The environment
tests matter more here than they do for a reviewer, because a coding agent is
the process an operator is most tempted to hand extra variables to.
"""

import os
import sys

import pytest

from review_loop.agent_process import (
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    DEFAULT_ENV_ALLOWLIST,
    AgentCommandError,
    SubprocessAgent,
    build_env,
    split_command,
)
from review_loop.fix_response import FIX_RESPONSE_BEGIN
from review_loop.fix_response_parser import parse
from review_loop.reviewer_process import SubprocessReviewer

from fix_fakes import FULL_SHA, response_text


def _python_agent(script: str, **kwargs) -> SubprocessAgent:
    return SubprocessAgent((sys.executable, "-c", script), **kwargs)


ECHO_RESPONSE = (
    "import sys\n"
    "sys.stdin.read()\n"
    f"print({response_text(preamble='')!r})\n"
)


# --------------------------------------------------------------------------
# The run itself
# --------------------------------------------------------------------------


def test_a_successful_agent_yields_its_stdout():
    run = _python_agent(ECHO_RESPONSE).invoke("prompt")

    assert run.ok
    assert parse(run.stdout)[0].fields["Target head SHA"] == FULL_SHA


def test_the_task_contract_reaches_the_agent_on_stdin():
    script = "import sys; sys.stdout.write(sys.stdin.read())"

    run = _python_agent(script).invoke("the bounded fix task")

    assert run.stdout == "the bounded fix task"


def test_a_nonzero_exit_is_a_failure_and_yields_no_stdout():
    """A failed agent must not hand downstream code something parseable."""
    script = f"import sys; print({response_text(preamble='')!r}); sys.exit(3)"

    run = _python_agent(script).invoke("prompt")

    assert not run.ok
    assert "coding agent exited 3" in run.failure
    assert run.stdout == ""


def test_a_timeout_is_a_failure_rather_than_an_empty_fix():
    script = "import time; time.sleep(5)"

    run = _python_agent(script, timeout=0.3).invoke("prompt")

    assert not run.ok
    assert "did not finish" in run.failure


def test_an_executable_that_does_not_exist_is_a_failure():
    run = SubprocessAgent(("./definitely-not-a-real-coding-agent",)).invoke("prompt")

    assert not run.ok
    assert "could not be run" in run.failure


def test_empty_stdout_is_not_a_fix_response():
    run = _python_agent("pass").invoke("prompt")

    assert run.ok
    assert run.stdout.strip() == ""


def test_oversized_output_is_a_failure():
    script = "print('x' * 200000)"

    run = _python_agent(script, max_output_bytes=1000).invoke("prompt")

    assert not run.ok
    assert "not a structured response" in run.failure


def test_stderr_is_captured_and_never_offered_as_stdout():
    """A response block written to stderr has not been produced."""
    script = (
        "import sys\n"
        f"sys.stderr.write({response_text(preamble='')!r})\n"
    )

    run = _python_agent(script).invoke("prompt")

    assert run.ok
    assert FIX_RESPONSE_BEGIN in run.stderr
    assert run.stdout.strip() == ""


def test_an_empty_command_is_refused():
    with pytest.raises(AgentCommandError, match="empty"):
        SubprocessAgent(())


def test_the_agent_runs_in_the_directory_it_is_given(tmp_path):
    script = "import os; print(os.getcwd())"

    run = _python_agent(script).invoke("prompt", cwd=str(tmp_path))

    assert os.path.realpath(run.stdout.strip()) == os.path.realpath(str(tmp_path))


# --------------------------------------------------------------------------
# No shell
# --------------------------------------------------------------------------


def test_shell_metacharacters_do_not_create_a_second_process(tmp_path):
    """`a; touch b` is four arguments to one program, not two commands."""
    marker = tmp_path / "should-not-exist"
    script = "import sys; print(sys.argv[1:])"
    argv = split_command(f"{sys.executable} -c '{script}' ; touch {marker}")

    run = SubprocessAgent(argv).invoke("prompt")

    assert run.ok
    assert run.stdout.strip() == repr([";", "touch", str(marker)])
    assert not marker.exists(), "the second command must never have run"


def test_quoting_rules_are_applied_but_nothing_is_expanded():
    argv = split_command("agent --flag 'one argument' $HOME")

    assert argv == ("agent", "--flag", "one argument", "$HOME")


def test_an_unparseable_command_is_refused():
    with pytest.raises(AgentCommandError, match="could not parse"):
        split_command("agent 'unterminated")


def test_an_empty_command_string_is_refused():
    with pytest.raises(AgentCommandError, match="empty"):
        split_command("   ")


# --------------------------------------------------------------------------
# The environment
# --------------------------------------------------------------------------


def test_only_the_allowlist_is_inherited():
    environ = {"PATH": "/bin", "HOME": "/home/x", "GH_TOKEN": "ghp_secret"}

    assert build_env(environ) == {"PATH": "/bin", "HOME": "/home/x"}


def test_a_named_variable_is_added_and_nothing_else_is():
    environ = {
        "PATH": "/bin",
        "UV_CACHE_DIR": "/tmp/uv",
        "GH_TOKEN": "ghp_secret",
        "AWS_SECRET_ACCESS_KEY": "aws",
    }

    built = build_env(environ, ("UV_CACHE_DIR",))

    assert built == {"PATH": "/bin", "UV_CACHE_DIR": "/tmp/uv"}


@pytest.mark.parametrize(
    "name", ["GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SLACK_BOT_TOKEN"]
)
def test_no_credential_variable_is_on_the_default_allowlist(name):
    assert name not in DEFAULT_ENV_ALLOWLIST


def test_an_unapproved_variable_is_not_visible_to_the_agent():
    """Run for real: the child asks its own environment, not a fixture."""
    script = "import os; print(os.environ.get('GH_TOKEN', 'absent'))"
    env = build_env({**os.environ, "GH_TOKEN": "ghp_secret"})

    run = _python_agent(script, env=env).invoke("prompt")

    assert run.stdout.strip() == "absent"


def test_an_approved_variable_is_visible_to_the_agent():
    script = "import os; print(os.environ.get('UV_CACHE_DIR', 'absent'))"
    env = build_env({**os.environ, "UV_CACHE_DIR": "/tmp/uv"}, ("UV_CACHE_DIR",))

    run = _python_agent(script, env=env).invoke("prompt")

    assert run.stdout.strip() == "/tmp/uv"


# --------------------------------------------------------------------------
# The two roles
# --------------------------------------------------------------------------


def test_the_agent_gets_a_longer_default_timeout_than_the_reviewer():
    """Making a change and running its tests is not the same work as reading."""
    assert DEFAULT_AGENT_TIMEOUT_SECONDS > SubprocessReviewer.default_timeout_seconds


def test_the_two_roles_are_named_apart_in_failures():
    script = "import sys; sys.exit(1)"

    agent_failure = _python_agent(script).invoke("p").failure
    reviewer_failure = SubprocessReviewer((sys.executable, "-c", script)).invoke("p").failure

    assert "coding agent" in agent_failure
    assert "reviewer" in reviewer_failure


def test_the_two_roles_share_one_environment_allowlist():
    from review_loop import reviewer_process

    assert DEFAULT_ENV_ALLOWLIST is reviewer_process.DEFAULT_ENV_ALLOWLIST
