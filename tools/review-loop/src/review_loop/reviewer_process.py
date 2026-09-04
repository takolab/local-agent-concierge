"""Run an externally configured reviewer command and capture what it wrote.

The reviewer is deliberately not a vendor integration. It is a command the
operator configures, invoked as an argument vector with ``shell=False``, given
the prompt on stdin and read back from stdout. That keeps three properties:

* **No new credential.** Whatever the operator already uses to run a reviewer
  is what runs here; this runner stores and requests nothing.
* **No shell.** The command is tokenised, never interpreted. Untrusted GitHub
  text -- a pull request title, a branch name, an author -- is never part of
  the command line at all: it reaches the reviewer only through the prompt on
  stdin, which is data.
* **A narrowed environment.** The child inherits an allowlist, not this
  process's environment, so a repository secret exported into the shell does
  not silently become the reviewer's to read.

This is emphatically **not** a sandbox, and the allowlist is not a capability
boundary. The reviewer runs as an ordinary child process with the invoking
user's filesystem permissions, and ``HOME`` and ``PATH`` are on the allowlist
because a real reviewer needs them -- which also means it can reach ``~/.
config/gh``, ``~/.ssh`` and any tool on the path. A reviewer command that
wanted to push, comment or merge could. "Read-only" is an instruction in the
prompt plus a property of the command the operator chooses to configure; the
guarantee this package enforces mechanically covers only the writes *it*
performs.

stdout is the only channel that can carry a verdict. stderr is captured for
diagnostics and never parsed: a reviewer that logs a well-formed verdict block
to stderr has not produced one.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

#: Environment variables the reviewer always receives. Anything else must be
#: named explicitly with ``--reviewer-env``.
DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER")

#: Reviewer stdout above this size is not a verdict, whatever else it is.
MAX_OUTPUT_BYTES = 1_000_000

DEFAULT_TIMEOUT_SECONDS = 900.0


class ReviewerCommandError(ValueError):
    """The configured reviewer command is not usable."""


@dataclass(frozen=True)
class ReviewerRun:
    """The result of one reviewer invocation.

    ``stdout`` is present only when the process succeeded; ``failure`` is
    present only when it did not. A failed reviewer never yields text that
    downstream code could mistake for a verdict.
    """

    stdout: str = ""
    stderr: str = ""
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def split_command(command: str) -> tuple[str, ...]:
    """Tokenise a configured reviewer command without a shell.

    ``shlex.split`` applies shell *quoting* rules only. It expands no
    variables, runs no substitution and honours no operators, so ``a; rm -rf
    b`` becomes four literal arguments to one program rather than two commands.
    """
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        raise ReviewerCommandError(f"could not parse the reviewer command: {exc}") from exc
    if not argv:
        raise ReviewerCommandError("the reviewer command is empty")
    return argv


def build_env(
    environ: dict[str, str], extra_names: tuple[str, ...] = ()
) -> dict[str, str]:
    """Build the reviewer's environment from an allowlist."""
    allowed = set(DEFAULT_ENV_ALLOWLIST) | set(extra_names)
    return {name: value for name, value in environ.items() if name in allowed}


class SubprocessReviewer:
    """Invoke one configured reviewer command per review turn."""

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        if not argv:
            raise ReviewerCommandError("the reviewer command is empty")
        self.argv = tuple(argv)
        self._timeout = timeout
        self._env = build_env(dict(os.environ)) if env is None else dict(env)
        self._cwd = cwd
        self._max_output_bytes = max_output_bytes

    def invoke(self, prompt: str) -> ReviewerRun:
        """Run the reviewer with ``prompt`` on stdin and capture stdout."""
        try:
            completed = subprocess.run(
                list(self.argv),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd,
                env=self._env,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ReviewerRun(
                failure=f"the reviewer did not finish within {self._timeout:g}s"
            )
        except (OSError, ValueError) as exc:
            return ReviewerRun(failure=f"the reviewer command could not be run: {exc}")

        stderr = completed.stderr or ""
        if completed.returncode != 0:
            return ReviewerRun(
                stderr=stderr,
                failure=f"the reviewer exited {completed.returncode}",
            )

        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", "replace")) > self._max_output_bytes:
            return ReviewerRun(
                stderr=stderr,
                failure=(
                    f"the reviewer wrote more than {self._max_output_bytes} bytes to "
                    "stdout, which is not a verdict"
                ),
            )
        return ReviewerRun(stdout=stdout, stderr=stderr)
