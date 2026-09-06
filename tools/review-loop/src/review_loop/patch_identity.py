"""One definition of what a candidate patch *is*, so two stages can agree.

The fix turn captures a diff; the push turn has to prove that the thing it is
about to commit is *that* diff and nothing else. Both claims are only as good
as their agreement about what "the diff" means, and a unified diff is not a
function of the content alone unless it is asked to be:

* ``index`` lines abbreviate blob hashes to a length derived from how many
  objects the repository holds, so the same change diffs differently in a
  fresh clone and in a long-lived one. ``--full-index`` removes the variable.
* ``diff.noprefix``, ``diff.mnemonicPrefix``, ``diff.algorithm``,
  ``diff.context`` and an external or textconv driver are all ordinary user
  configuration, and each changes the bytes without changing the change.

So every diff whose bytes are load-bearing goes through :func:`diff_argv`,
which pins each of those explicitly rather than inheriting them. The result
is that the digest of a patch is a property of the change, comparable across
two invocations, two worktrees and two machines.

The digest itself is deliberately of the **patch text**, not of a resulting
tree. A tree hash would answer "is the end state the same?", which is a
weaker question: two different patches can produce one tree, and the claim
this pipeline needs to carry forward is that the exact reviewed-and-validated
diff is the one being committed. The commit's own diff is re-digested after
the commit exists, so the identity is checked against git's account of the
commit rather than against the runner's memory of what it applied.
"""

from __future__ import annotations

import hashlib

from .reviewer_workspace import DEFAULT_GIT_TIMEOUT_SECONDS, run_git

#: Configuration this runner refuses to inherit, because each entry changes
#: the bytes of a diff without changing what the diff says.
_DIFF_CONFIG: tuple[str, ...] = (
    "-c", "core.abbrev=40",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.algorithm=myers",
)

#: Flags that pin the rest of the rendering. ``--binary`` keeps a binary
#: change representable at all; ``--full-index`` makes the ``index`` lines
#: exact; the two ``--no-*`` flags refuse any diff driver the repository's
#: own ``.gitattributes`` might otherwise install.
_DIFF_FLAGS: tuple[str, ...] = (
    "--binary",
    "--full-index",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "-U3",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def diff_argv(*revisions: str) -> list[str]:
    """Argument vector for a diff whose bytes depend only on the change.

    ``diff_argv("HEAD")`` diffs the working tree against ``HEAD``;
    ``diff_argv(parent, child)`` diffs one commit against another. Both are
    used, and they must render identically for the same change -- which they
    do, and which the tests pin.
    """
    return [*_DIFF_CONFIG, "diff", *_DIFF_FLAGS, *revisions]


def capture_patch(
    cwd: str, *revisions: str, timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS
) -> str:
    """Render a canonical diff and return every byte of it.

    The single reason this is a function rather than two lines at each call
    site is ``strip=False``. :func:`review_loop.reviewer_workspace.run_git`
    strips by default, which is right for the one-line answers most callers
    want and wrong here in a way that is silent until it is not: stripping
    removes the newline after the last hunk line, and ``git apply`` rejects
    the result as a corrupt patch. Every diff whose bytes are load-bearing
    goes through here, so that cannot be got wrong once per call site.
    """
    return run_git(diff_argv(*revisions), cwd=cwd, timeout=timeout, strip=False)


def patch_digest(patch: str) -> str:
    """The identity of a candidate patch: SHA-256 over its UTF-8 bytes."""
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def digest_bytes(data: bytes) -> str:
    """The same identity, for a patch read back from a file."""
    return hashlib.sha256(data).hexdigest()
