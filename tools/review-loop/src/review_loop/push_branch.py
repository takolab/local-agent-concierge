"""Where the push authority comes from, and everything it is not.

This is the narrowest question in the slice and the one worth being most
pedantic about: **which ref may this runner write to?**

The answer is derived from one source only -- the pull request object GitHub
returns for the number recorded in the validated fix handoff. Nothing else in
the pipeline is allowed near it. Not the review JSON's text, not a finding's
``Location``, not the Coding Agent's output, not a patch header, not a command
line flag. Those are all inputs that an agent or a reviewer can influence, and
a branch name is not the kind of thing that may be influenced: it is the
difference between updating a pull request and rewriting ``master``.

So the derivation is: ``pulls/{n}`` -> ``head.ref``, and then a series of
refusals rather than a series of allowances.

* **The pull request must be open.** A closed one is not a thing to push to.
* **The head must live in this repository.** A fork's ``head.ref`` names a
  branch in *someone else's* repository, and pushing that name to ``origin``
  would create or move a same-named branch here instead -- a write to a ref
  the pull request never referred to. Fork pull requests are refused, not
  redirected.
* **The head must not be the base**, and **must not be the repository's
  default branch**. Either would mean the "pull request branch" is the branch
  the pull request is merging into. GitHub does not create such a pull
  request, so seeing one means the model is wrong somewhere -- and the failure
  it protects against is the one nobody recovers from.
* **The name must be an ordinary branch name.** ``refs/``-qualified names,
  ``@{``, ``..``, leading dashes, ``.lock`` suffixes and control characters
  are all refused. The refspec is built by this module, so a name that could
  be read as an option or as a different ref never reaches ``git push``.

A ref name is only half of a destination, though, and the other half was
missing at first: **which repository receives that ref update.** The branch
came from GitHub, but the repository came from ``--git-remote``, which is
operator-controlled -- so pointing the command at a mirror or a fork remote
that happened to hold the same branch at the same commit would push there,
while every message said "this repository". :func:`check_remote_repository`
closes that: every URL git reports for the chosen remote must name the
repository the validated handoff describes.

The rule it applies is deliberately stated rather than implied:

* A URL with a **host** must be a GitHub host, and its ``owner/name`` must be
  the target repository. A remote pointing at ``someone/fork``, or at another
  forge entirely, is refused -- those are the realistic ways to get this
  wrong.
* A URL with **no host** -- a local path, the shape every test and every
  local mirror uses -- is accepted when its final two path segments name the
  target repository. This runner cannot prove a local path is GitHub, and it
  says so rather than pretending: an operator who has constructed a path that
  ends in ``<owner>/<name>.git`` in order to redirect this push is already
  able to push there by hand.

``git remote get-url`` reports the *effective* URL, with any
``url.<base>.insteadOf`` rewriting already applied, so a rewrite cannot hide
behind this check.

What this module deliberately does *not* do is decide whether the push should
happen. It answers only "if a push happens, where to", and a caller that
never calls it cannot push at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import FULL_SHA_PATTERN

#: Ordinary branch names only: letters, digits, and the punctuation git
#: itself allows in a ref, with the ambiguous shapes excluded below.
_BRANCH_CHARS = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

#: Refused outright wherever they appear in the name.
_BRANCH_FORBIDDEN = ("..", "@{", "//", "^", ":", "?", "*", "[", "\\", " ", "~")

MAX_BRANCH_CHARS = 255

#: Hosts whose ``owner/name`` path this runner will accept as naming the
#: repository the GitHub client is reading. Anything else is a different
#: system, whatever its contents look like.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})

#: ``scheme://[user[:pass]@]host[:port]/path`` and the ``user@host:path`` form
#: git accepts for ssh. Both end in the path this runner reads the repository
#: identity from.
_SCHEME_URL = re.compile(
    r"\A(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://"
    r"(?:[^/@]*@)?"
    r"(?P<host>[^/:]+)"
    r"(?::\d+)?"
    r"/(?P<path>.*)\Z"
)
_SCP_URL = re.compile(r"\A(?:[^/@]+@)?(?P<host>[^/:]+):(?!/)(?P<path>.+)\Z")


class BranchAuthorityError(ValueError):
    """The branch this runner would push to cannot be established safely."""


@dataclass(frozen=True)
class PushTarget:
    """The one ref this run is permitted to write, and what it was derived from."""

    repo: str
    number: int
    #: The pull request's head branch, as a plain name. The refspec is built
    #: from it here and nowhere else.
    branch: str
    base_ref: str
    default_branch: str
    #: The head SHA GitHub reported when this was resolved.
    head_sha: str

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    def refspec(self, sha: str) -> str:
        """A fast-forward push of one exact commit to one exact branch.

        No leading ``+``, so a non-fast-forward is rejected by the remote
        rather than by this runner's good intentions. The commit is named by
        its full SHA rather than by ``HEAD``, so what is pushed is the commit
        this runner verified and not whatever the worktree points at now.
        """
        if not FULL_SHA_PATTERN.match(sha):
            raise BranchAuthorityError(
                f"refusing to build a refspec for {sha!r}, which is not an exact "
                "40-character commit"
            )
        return f"{sha}:{self.ref}"


def check_branch_name(name: object) -> str:
    """Return ``name`` if it is an ordinary, unambiguous branch name."""
    if not isinstance(name, str) or not name:
        raise BranchAuthorityError(
            f"GitHub reported the pull request's head branch as {name!r}, which is "
            "not a branch name"
        )
    if len(name) > MAX_BRANCH_CHARS:
        raise BranchAuthorityError(
            f"the pull request's head branch is {len(name)} characters, above the "
            f"{MAX_BRANCH_CHARS} this runner will push to"
        )
    for forbidden in _BRANCH_FORBIDDEN:
        if forbidden in name:
            raise BranchAuthorityError(
                f"the pull request's head branch {name!r} contains {forbidden!r}, "
                "which git reads as something other than a plain branch name"
            )
    if name.startswith("refs/") or name.endswith("/") or name.endswith(".lock"):
        raise BranchAuthorityError(
            f"the pull request's head branch {name!r} is not a plain branch name; "
            "this runner builds the refs/heads/ prefix itself"
        )
    if not _BRANCH_CHARS.match(name):
        raise BranchAuthorityError(
            f"the pull request's head branch {name!r} contains a character this "
            "runner will not put in a refspec"
        )
    if name == "HEAD" or any(segment.startswith(".") for segment in name.split("/")):
        raise BranchAuthorityError(
            f"the pull request's head branch {name!r} is a name git resolves "
            "specially rather than an ordinary branch"
        )
    return name


def resolve(payload: dict, *, repo: str, number: int) -> PushTarget:
    """Derive the single writable ref from GitHub's own pull request object."""
    if not isinstance(payload, dict):
        raise BranchAuthorityError(f"pull request #{number} returned no object")

    reported = payload.get("number")
    if reported != number:
        raise BranchAuthorityError(
            f"the pull request object reports #{reported!r}, not the #{number} the "
            "fix handoff describes"
        )

    state = payload.get("state")
    if state != "open":
        raise BranchAuthorityError(
            f"pull request #{number} is {state or 'in an unknown state'}; a closed "
            "pull request is not a branch this runner pushes to"
        )

    head = payload.get("head") or {}
    base = payload.get("base") or {}
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise BranchAuthorityError(
            f"pull request #{number} has no readable head or base"
        )

    head_repo = (head.get("repo") or {}).get("full_name")
    if not isinstance(head_repo, str) or not head_repo:
        raise BranchAuthorityError(
            f"pull request #{number} does not say which repository its head branch "
            "lives in, so a branch name from it cannot be trusted to name a branch "
            "here"
        )
    if head_repo != repo:
        raise BranchAuthorityError(
            f"pull request #{number}'s head branch lives in {head_repo}, not "
            f"{repo}. Pushing its name to this repository's remote would write a "
            "different ref from the one the pull request refers to, so a "
            "cross-repository head is refused"
        )

    branch = check_branch_name(head.get("ref"))
    base_ref = base.get("ref")
    if not isinstance(base_ref, str) or not base_ref:
        raise BranchAuthorityError(
            f"pull request #{number} does not name a base branch"
        )
    if branch == base_ref:
        raise BranchAuthorityError(
            f"pull request #{number} reports the same branch {branch!r} as head and "
            "base; pushing to it would write the branch being merged into"
        )

    default_branch = (base.get("repo") or {}).get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise BranchAuthorityError(
            f"pull request #{number} does not say what this repository's default "
            "branch is, so this runner cannot rule out pushing to it"
        )
    if branch == default_branch:
        raise BranchAuthorityError(
            f"pull request #{number}'s head branch is {branch!r}, which is this "
            "repository's default branch. This runner does not push to a default "
            "branch under any circumstances"
        )

    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not FULL_SHA_PATTERN.match(head_sha):
        raise BranchAuthorityError(
            f"pull request #{number} reports head SHA {head_sha!r}, which is not an "
            "exact 40-character commit"
        )

    return PushTarget(
        repo=repo,
        number=number,
        branch=branch,
        base_ref=base_ref,
        default_branch=default_branch,
        head_sha=head_sha,
    )


# --------------------------------------------------------------------------
# Which repository receives the push
# --------------------------------------------------------------------------


def _identity_from_path(path: str) -> str | None:
    """Read ``owner/name`` from a repository URL's path component."""
    trimmed = path.strip().strip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    segments = [segment for segment in trimmed.split("/") if segment and segment != "."]
    if len(segments) < 2:
        return None
    owner, name = segments[-2], segments[-1]
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def repository_from_url(url: str) -> tuple[str | None, str | None]:
    """Return ``(host, owner/name)`` for a git remote URL.

    ``host`` is ``None`` for a plain local path, which is a real and common
    kind of remote rather than a malformed one. Either element is ``None``
    when this runner cannot read it, and a caller must treat that as "not
    proven" rather than as "fine".
    """
    text = (url or "").strip()
    if not text:
        return None, None

    if text.startswith("file://"):
        return None, _identity_from_path(text[len("file://") :])

    scheme = _SCHEME_URL.match(text)
    if scheme:
        return scheme.group("host").lower(), _identity_from_path(scheme.group("path"))

    # An scp-style `host:path` is only that if it is not a Windows drive
    # letter or an absolute local path with a colon in it.
    if "://" not in text and not text.startswith(("/", ".", "~")):
        scp = _SCP_URL.match(text)
        if scp:
            return scp.group("host").lower(), _identity_from_path(scp.group("path"))

    return None, _identity_from_path(text)


def check_remote_repository(
    urls: tuple[str, ...], *, expected_repo: str, remote: str
) -> None:
    """Raise unless every URL for ``remote`` names ``expected_repo``.

    Every URL, not the first one: a remote can carry a separate ``pushurl``,
    and the one that decides where a push lands is not the one that decides
    where a fetch comes from. Requiring them all to agree means the check
    cannot be satisfied by whichever one happens to be read first.
    """
    if not urls:
        raise BranchAuthorityError(
            f"the remote {remote!r} reports no URL, so which repository this push "
            "would reach cannot be established"
        )

    for url in urls:
        host, identity = repository_from_url(url)
        if identity is None:
            raise BranchAuthorityError(
                f"the remote {remote!r} resolves to {url!r}, which this runner "
                "cannot read as a repository. It will not push to a destination "
                "it cannot name"
            )
        if host is not None and host not in GITHUB_HOSTS:
            raise BranchAuthorityError(
                f"the remote {remote!r} resolves to {url!r}, whose host is not "
                f"GitHub. The pull request was read from GitHub, so pushing there "
                "would update a ref in a different system from the one that "
                "authorised it"
            )
        if identity.casefold() != expected_repo.casefold():
            raise BranchAuthorityError(
                f"the remote {remote!r} resolves to {url!r}, which names "
                f"{identity}, not the {expected_repo} this fix was validated "
                "against. Pushing there would update a branch in another "
                "repository that merely holds the same history"
            )
