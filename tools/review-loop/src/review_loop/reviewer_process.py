"""Run an externally configured *reviewer* command and capture what it wrote.

The reviewer is deliberately not a vendor integration. It is a command the
operator configures, invoked as an argument vector with ``shell=False``, given
the prompt on stdin and read back from stdout.

The mechanism -- and the security properties that come with it: no shell, no
new credential, an allowlisted environment, a bounded time and output size --
lives in :mod:`review_loop.bounded_process`, because the Coding Agent of the
routing slice is started the same way and the argument must not be made
twice. This module is the *reviewer's* name for it, and the place its role is
stated:

**The reviewer is read-only, and that is an instruction, not an enforcement.**
It runs as an ordinary child process with the invoking user's filesystem
permissions, and ``HOME`` and ``PATH`` are on the allowlist because a real
reviewer needs them -- which also means it can reach ``~/.config/gh``,
``~/.ssh`` and any tool on the path. A reviewer command that wanted to push,
comment or merge could. "Read-only" is a line in the prompt plus a property of
the command the operator chooses to configure; the guarantee this package
enforces mechanically covers only the writes *it* performs.

stdout is the only channel that can carry a verdict. stderr is captured for
diagnostics and never parsed: a reviewer that logs a well-formed verdict block
to stderr has not produced one.
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
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_OUTPUT_BYTES",
    "ReviewerCommandError",
    "ReviewerRun",
    "SubprocessReviewer",
    "build_env",
    "split_command",
]

DEFAULT_TIMEOUT_SECONDS = 900.0

#: The reviewer's names for the shared vocabulary. Aliases rather than
#: parallel definitions: a reviewer failure and an agent failure are the same
#: value object, and only the role differs.
ReviewerCommandError = CommandError
ReviewerRun = CommandRun


class SubprocessReviewer(BoundedCommand):
    """Invoke one configured reviewer command per review turn."""

    default_timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    role = "reviewer"
