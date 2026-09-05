"""Run one externally configured command, bounded, with no shell.

This is the mechanism behind every agent this runner starts -- the
Independent Reviewer (PR #29) and now the Coding Agent. It lives in one place
deliberately: the properties below are the security argument for *both*
roles, and an argument made twice is an argument that can drift.

* **No new credential.** Whatever the operator already uses to run an agent is
  what runs here; this package stores and requests nothing.
* **No shell.** The command is tokenised, never interpreted. Untrusted text --
  a pull request title, a branch name, a reviewer's finding -- is never part
  of the command line at all: it reaches the child only through the prompt on
  stdin, which is data.
* **A narrowed environment.** The child inherits an allowlist, not this
  process's environment, so a repository secret exported into the shell does
  not silently become the agent's to read.
* **Bounded output and time.** stdout above a byte limit is not a contract
  response, whatever else it is, and a child that never finishes is a failure
  rather than a hang.

stdout is the only channel that can carry a structured response. stderr is
captured for diagnostics and never parsed: an agent that logs a well-formed
response block to stderr has not produced one.

This is emphatically **not** a sandbox, and the allowlist is not a capability
boundary. The child runs as an ordinary process with the invoking user's
filesystem permissions, and ``HOME`` and ``PATH`` are on the allowlist
because a real agent needs them -- which also means it can reach
``~/.config/gh``, ``~/.ssh`` and any tool on the path. What this package
guarantees mechanically covers only the actions *it* performs.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

#: Environment variables an agent always receives. Anything else must be
#: named explicitly by the operator.
DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER")

#: Child stdout above this size is not a structured response.
MAX_OUTPUT_BYTES = 1_000_000


class CommandError(ValueError):
    """The configured command is not usable."""


@dataclass(frozen=True)
class CommandRun:
    """The result of one invocation.

    ``stdout`` is present only when the process succeeded; ``failure`` is
    present only when it did not. A failed run never yields text that
    downstream code could mistake for a contract response.
    """

    stdout: str = ""
    stderr: str = ""
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def split_command(command: str) -> tuple[str, ...]:
    """Tokenise a configured command without a shell.

    ``shlex.split`` applies shell *quoting* rules only. It expands no
    variables, runs no substitution and honours no operators, so ``a; rm -rf
    b`` becomes four literal arguments to one program rather than two commands.
    """
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        raise CommandError(f"could not parse the command: {exc}") from exc
    if not argv:
        raise CommandError("the command is empty")
    return argv


def build_env(
    environ: dict[str, str], extra_names: tuple[str, ...] = ()
) -> dict[str, str]:
    """Build a child environment from an allowlist."""
    allowed = set(DEFAULT_ENV_ALLOWLIST) | set(extra_names)
    return {name: value for name, value in environ.items() if name in allowed}


class BoundedCommand:
    """One configured command, invoked with a prompt on stdin.

    Subclasses exist only to name a role and its default timeout. The
    mechanism -- and every property listed in this module's docstring -- is
    the same for all of them.
    """

    #: Overridden per role: a reviewer reads, a coding agent reads and edits.
    default_timeout_seconds = 900.0

    #: What the role is called in failure messages.
    role = "agent"

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        if not argv:
            raise CommandError(f"the {self.role} command is empty")
        self.argv = tuple(argv)
        self._timeout = self.default_timeout_seconds if timeout is None else timeout
        self._env = build_env(dict(os.environ)) if env is None else dict(env)
        self._cwd = cwd
        self._max_output_bytes = max_output_bytes

    def invoke(self, prompt: str, *, cwd: str | None = None) -> CommandRun:
        """Run the command with ``prompt`` on stdin and capture stdout.

        ``cwd`` overrides the directory configured at construction. The caller
        that supplies it has already verified that the directory is a checkout
        of the commit under review; see
        :mod:`review_loop.reviewer_workspace`.
        """
        try:
            completed = subprocess.run(
                list(self.argv),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd if cwd is None else cwd,
                env=self._env,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandRun(
                failure=f"the {self.role} did not finish within {self._timeout:g}s"
            )
        except (OSError, ValueError) as exc:
            return CommandRun(failure=f"the {self.role} command could not be run: {exc}")

        stderr = completed.stderr or ""
        if completed.returncode != 0:
            return CommandRun(
                stderr=stderr,
                failure=f"the {self.role} exited {completed.returncode}",
            )

        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", "replace")) > self._max_output_bytes:
            return CommandRun(
                stderr=stderr,
                failure=(
                    f"the {self.role} wrote more than {self._max_output_bytes} bytes "
                    "to stdout, which is not a structured response"
                ),
            )
        return CommandRun(stdout=stdout, stderr=stderr)
