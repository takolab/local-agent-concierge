"""Run an externally configured *Coding Agent* command.

Same mechanism as the reviewer -- :mod:`review_loop.bounded_process` -- and a
deliberately different role, so the two are named apart everywhere an operator
or a reader might otherwise conflate them:

* the **reviewer** finds problems in a tree it is told not to touch;
* the **Coding Agent** consumes findings that are already validated, and makes
  a bounded edit inside a worktree prepared for exactly that purpose.

Two settings differ, and only two:

* **A longer default timeout.** Reading a diff and writing a verdict is not
  the same amount of work as making a change and running the tests around it.
* **Its own environment allowlist entries**, supplied by the operator. A
  coding agent legitimately needs more than a reviewer -- a language runtime's
  cache directory, a package index setting -- but it gets exactly what is
  named, never the invoking shell's environment.

What does **not** differ is the authority boundary, and it is worth stating
in the agent's own terms because the temptation to widen it is greater here.
This package gives the agent no GitHub credential, no push path, no comment
path and no merge path; the fix subcommand that starts it makes no GitHub
request at all. But the agent is still an ordinary child process running as
the invoking user. A worktree is where it is *pointed*, not a wall around it.
If the operator's machine has a usable ``gh`` login and the agent decides to
use it, nothing here would stop it -- which is why the writable-workspace
inspection in :mod:`review_loop.agent_workspace` verifies what actually
changed rather than believing what the agent reports.
"""

from __future__ import annotations

from .bounded_process import (
    DEFAULT_ENV_ALLOWLIST,
    MAX_OUTPUT_BYTES,
    BoundedCommand,
    CommandError,
    CommandRun,
    build_env,
    split_command,
)

__all__ = [
    "DEFAULT_AGENT_TIMEOUT_SECONDS",
    "DEFAULT_ENV_ALLOWLIST",
    "MAX_OUTPUT_BYTES",
    "AgentCommandError",
    "AgentRun",
    "SubprocessAgent",
    "build_env",
    "split_command",
]

#: Half an hour. A bounded fix that has not finished by then is not bounded.
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800.0

AgentCommandError = CommandError
AgentRun = CommandRun


class SubprocessAgent(BoundedCommand):
    """Invoke one configured Coding Agent command per fix turn."""

    default_timeout_seconds = DEFAULT_AGENT_TIMEOUT_SECONDS
    role = "coding agent"
