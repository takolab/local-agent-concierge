# Review loop runner

A local command that runs one Independent AI Review turn against a pull
request, records the result only when it can prove which exact pull request
state that review describes, and routes the findings that review produced to
one bounded Coding Agent turn against that same exact state.

It has three commands.

**`review-loop --pr N`** answers two questions, read-only:

1. **Which exact commit would be reviewed?** — the pull request's current
   40-character head SHA, not its merge commit and not its base.
2. **Is that exact commit's CI in a state where a review may start?**

**`review-loop review --pr N`** uses that answer to run the review itself:
capture the verified target, invoke an independent read-only reviewer, validate
the Structured Verdict it returns, re-verify that the target has not moved, and
record the result as one `## Independent AI Review` comment.

**`review-loop fix --review-json <file>`** takes that review's own JSON output
and routes its open findings to one bounded Coding Agent turn: a dedicated
writable worktree at the reviewed commit, an explicit allowed scope derived
from the findings, a Structured Fix Response validated against the working
tree, and a patch. It makes **no GitHub request at all** and commits nothing.

Everything after that — committing the fix, pushing it, waiting for CI,
re-review, merge decisions — is not here. This is one review turn and one
bounded fix turn, both bound to one verified state, with the human keeping
every decision about what happens to the result.

## Verification: `review-loop --pr N`

```bash
pip install -e "tools/review-loop[test]"
review-loop --pr 28 --dry-run
```

The package requires **Python 3.12 or newer**. On a machine whose default
`python3` is older, that `pip install` fails with `requires a different
Python`; create a 3.12 environment first (`uv venv --python 3.12`, `pyenv`,
or a distribution package) and install into it. `git` and the `gh` CLI must
be on `PATH`.

```text
PR:                   #28 (feat/review-loop-pr-head-ci-verification -> master)
Head SHA:             a0794113e82591dbee912da0826a004ba91e166f  [a079411]
Head stable:          Yes
CI merge base:        6a2f7cfe8cc8cb4af22b7824d1c70e6fce389bb8
Merge base current:   Yes
Baseline workflows:   .github/workflows/pytest.yml
Observed workflows:
  .github/workflows/pytest.yml [REQUIRED] name='Python tests' run=33797660279 attempt=1 event=pull_request status=completed conclusion=success
  .github/workflows/review-loop.yml [CONDITIONAL] name='Review loop runner tests' run=33797660398 attempt=1 event=pull_request status=completed conclusion=success
CI verdict:           READY
Reason:
  - .github/workflows/pytest.yml (run 33797660279 attempt 1) succeeded
  - .github/workflows/review-loop.yml (run 33797660398 attempt 1) succeeded
  - .github/workflows/agent-contracts.yml did not run: this diff misses its path filter
  - .github/workflows/orchestrator.yml did not run: this diff misses its path filter
GitHub write performed: No
```

Options: `--pr` (required), `--repo owner/name` (defaults to the current git
remote), `--dry-run`, `--json`. Run `review-loop --help` for the exit codes.

`--dry-run` is a no-op for this command, because verification is read-only in
every mode. It is meaningful for `review-loop review`, below.

## CI verdict semantics

| Verdict | Exit | Meaning |
| --- | --- | --- |
| `READY` | 0 | The pull request is open, its head resolved and did not move, the base its CI merged onto is still the base branch tip, the baseline workflow produced a `pull_request` run for that exact commit, every workflow that should have run did, and all of them succeeded. |
| `PENDING` | 10 | A relevant run for the exact head is `queued`, `requested`, `waiting`, `in_progress` or `pending`. |
| `FAILED` | 11 | A relevant run completed with `failure`, `timed_out`, `startup_failure`, `cancelled`, `action_required` or `stale`. |
| `AMBIGUOUS` | 12 | CI state could not be determined safely — see below. |
| `STALE_TARGET` | 13 | The head moved during verification, or the base its CI merged onto is no longer the base branch tip. |
| `API_ERROR` | 20 | GitHub could not be queried. |
| — | 2 | CLI usage error. |

Only exit code `0` means a review may be started, so `review-loop --pr N` is
directly usable as a shell condition. Every other code is a distinct non-zero
value, so an automation can also branch on *why* it is not ready.

The runner fails closed: anything it cannot explain becomes `AMBIGUOUS` rather
than collapsing into `READY`. `AMBIGUOUS` covers a missing baseline run, a
path-filtered workflow that the diff should have triggered but which produced
no run, a workflow whose `pull_request` trigger or path filter could not be
interpreted, a run for a workflow absent from the configuration at that commit,
one workflow path reported under several workflow ids, a run that belongs to a
different pull request or carries no association at all, runs that merged onto
different bases, an unrecognised status or conclusion, runs returned for a
commit other than the one queried, and a pull request that is not open.

### How workflows are identified

By **workflow file path**, together with the numeric workflow id. Never by job
or check display name: all of this repository's workflows expose a single job
named `test`, so `GET /commits/{sha}/check-runs` returns several entries named
`test` that cannot be told apart.

### Which workflows must have run

The workflow files are read at **the exact head SHA under review**, which is
the configuration GitHub itself uses to decide what a `pull_request` event
starts. Each is classified as:

* **`REQUIRED`** — triggered by `pull_request` with no `paths`/`paths-ignore`
  filter. It always runs, so its absence is never explainable by the diff. At
  least one such baseline workflow must exist *and* have produced a run for the
  exact head, or the verdict is `AMBIGUOUS`. A green path-filtered workflow on
  its own is not evidence that the commit was built.
* **`CONDITIONAL`** — path-filtered. Its absence is checked against this pull
  request's own changed files: if the diff matches the filter the run must
  exist, if the diff misses it the absence is explained, and if the filter
  cannot be interpreted the absence is unexplained. "A filter exists" is not
  the same claim as "this diff misses it", and only the second excuses a
  missing run.
* **`NOT_EXPECTED`** — no `pull_request` trigger for this base branch.
* **`UNKNOWN`** — the trigger block could not be interpreted → `AMBIGUOUS`.

No workflow count is ever assumed. Path filters make the number of runs vary
between pull requests: at the time of writing, PRs #26 and #27 each produced
two runs out of three configured workflows.

The matcher's invariant is not "support GitHub's glob syntax". It is **never
report a miss unless GitHub would certainly agree**, because a false miss
explains away a workflow that should have run — which is precisely how a false
`READY` is produced. Only shapes settled by GitHub's own documented examples
are decided:

* patterns with no `**` — `*` matches within one path segment
* a literal prefix with a trailing `/**`, such as `services/orchestrator/**`

Everything else is undecidable: `**` in any interior or leading position
(`docs/**/*.md`, `**/README.md`, `**.js`), and `?`, `+`, `[]` or a leading `!`
anywhere. GitHub does support those richer forms — its documented example for
`docs/**/*.md` lists `docs/README.md`, with zero intervening directories, and
`**/README.md` matches a root-level `README.md`. This slice deliberately models
only the narrower subset the repository actually uses: more matcher surface is
more to get wrong, and a wrong miss is the expensive direction. Extending the
subset is a deliberate later change, made against fixtures drawn from those
documented examples.

A filter is consulted **only when its workflow produced no run**, so an
undecidable pattern costs nothing whenever the evidence exists anyway. Every
path filter currently in this repository falls inside the decidable set.

### Branch filters

`branches` and `branches-ignore` together are not valid GitHub configuration
for one event, so a workflow specifying both is `UNKNOWN` — evaluating either
key would otherwise yield a confident `NOT_EXPECTED` and excuse a missing run.
The same already holds for `paths` with `paths-ignore`.

Otherwise `branches` and `branches-ignore` are matched by **literal equality
only**.
GitHub's branch globs are not Python's: its `*` does not span `/` — which is
why `releases/**` exists as a separate documented form — and `!` negates in
order. `fnmatch` disagrees on both, and it would report `release/*` as matching
`release/1.0/hotfix`. A wrong "this workflow does not apply to this branch"
silently excuses a missing run, so any pattern containing `*`, `?`, `[]`, `!`
or `+` makes the workflow `UNKNOWN`, and therefore `AMBIGUOUS`. This
repository uses `branches: [master]` throughout.

### What a `pull_request` run actually tested

With a plain `actions/checkout`, a `pull_request` run does not build the head
commit. It builds `refs/pull/N/merge` — the head merged onto the base at that
moment. This is directly observable: run `33797660398` on PR #28 reports head
`a0794113…`, while its own checkout log says
`HEAD is now at 73d15f6 Merge a0794113… into 6a2f7cfe…`.

Three consequences are handled rather than assumed away:

* **Only `pull_request` runs are evidence.** A `push` or `workflow_dispatch`
  run on the same commit built the head tree, which is a different tree. Such
  runs are displayed but can neither satisfy the baseline requirement nor
  override a `pull_request` run.
* **Runs must belong to this pull request.** A run's `pull_requests` array is
  checked against the target number; sharing a head SHA is not enough.
* **The merge context can go stale while the head does not.** The base branch
  tip is re-read after evidence is collected. If CI merged onto a base that is
  no longer the tip, the validated merge no longer exists and the verdict is
  `STALE_TARGET`.

GitHub empties a run's `pull_requests` array once the pull request closes, so
the merge context cannot be established for a merged pull request. A closed
pull request is not a review target anyway, so it reports `AMBIGUOUS`. Merged
pull requests remain useful for exercising retrieval, full-SHA handling and
normalization, but they will not report `READY`.

### Reruns and re-triggers

Several runs can exist for one exact commit, and they need different
tie-breakers:

* **Re-running** a workflow keeps the same `run_id` and increments
  `run_attempt`. The highest attempt wins, so an older failed attempt never
  outvotes a newer successful one.
* **Re-triggering** creates a new `run_id`. The most recently created run wins,
  so an older successful run never hides a newer failed or still-running one.

Superseded run ids are printed, so a rerun is visible rather than silent.

### Why not the commit status API

`GET /commits/{sha}/status` is not used. For this repository it reports
`state: "pending"` with `total_count: 0` even for commits whose Actions runs
all succeeded, because nothing here publishes classic commit statuses. Treating
it as authoritative would make every commit look permanently pending. CI here
is GitHub Actions only, so the runner is bounded to the Actions API; there is
no generic CI-provider abstraction.

### Why the full SHA matters

`GET /actions/runs?head_sha=` matches the exact 40-character SHA only. Given an
abbreviated SHA it answers **HTTP 200 with `total_count: 0`** — indistinguishable
from a commit that genuinely has no CI. An abbreviated SHA is therefore rejected
at the client boundary rather than queried. Short SHAs appear in output for
readability and are never used as an identity.

### Stale targets

Both ends of the tested merge are re-read after the evidence has been
collected. If the **head** moved, the evidence describes a commit that is no
longer the review target. If the **base** moved, the head is still the review
target but the merge CI validated no longer exists. Either one yields
`STALE_TARGET`; a green result for a superseded commit or a superseded merge is
never reported as `READY`.

## GitHub authentication

Everything shells out to the already-authenticated `gh` CLI (`gh auth login`).
No new credential is introduced: no PAT, no GitHub App, no secret, and nothing
stored in the repository.

The verification client is read-only by construction. Every request it makes
goes through a single `gh api` call site that hard-codes `--method GET`; no
caller supplies an HTTP method, and no write endpoint is referenced anywhere in
it. The one write this tool can perform — creating a review comment — lives in
a separate module, described under [GitHub writes](#github-writes). Tests
assert both halves at the source level.

## The review turn: `review-loop review --pr N`

```bash
review-loop review --pr 29 --reviewer-command "my-reviewer --read-only" --dry-run
```

```text
PR:                   #29 (base master)
Head SHA:             3b514700c1c2c257a39a7037f1a21ca5b9064106  [3b51470]
CI merge base:        6a2f7cfe8cc8cb4af22b7824d1c70e6fce389bb8
CI verification:      READY
Reviewer:             my-reviewer --read-only
Reviewer invoked:     Yes
Verdict:              round=1 recommendation=changes_requested open=1 (Blocking 0 / Major 1 / Minor 0)
Reviewed head SHA:    3b514700c1c2c257a39a7037f1a21ca5b9064106 (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
Reason:
  - dry run: the review is valid and would be recorded
GitHub write performed: No
```

The steps, in order, and why the order is the design:

1. **Verify** with the command above. Anything but `READY` stops here and *no
   reviewer is started*: a review of a commit whose CI state is unknown would
   produce a record claiming more than was verified.
2. **Capture the target** — repository, pull request number, exact head SHA,
   base branch, the base commit CI merged onto, and the runs that were green.
3. **Check for an existing record** before paying for a review that could not
   be posted anyway.
4. **Bind the reviewer's working directory** to that exact SHA — prepare a
   detached worktree at it, or verify the one you supplied — then **run the
   reviewer** there. A reviewer reading another tree would echo the target
   SHA back from its prompt, and every later check would agree with it.
5. **Parse and validate** its output against the Structured Verdict contract.
6. **Re-verify the target**, because a reviewer takes minutes and a pull
   request can move in minutes.
7. **Check for an existing record again**, immediately before writing.
8. **Post exactly one comment.**

| Outcome | Exit | Meaning |
| --- | --- | --- |
| `REVIEW_VALID` | 0 | A validated review was recorded — or, under `--dry-run`, would have been. |
| `COMMENT_ALREADY_EXISTS` | 0 | This exact review is already on the pull request. Nothing was written. |
| `TARGET_NOT_READY` | 10/11/12/13/20 | Verification did not report `READY`; the exit code is that verdict's own. No reviewer ran. |
| `API_ERROR` | 20 | GitHub could not be queried. |
| `REVIEWER_FAILED` | 30 | The reviewer exited non-zero, timed out, or could not be run. |
| `REVIEWER_WORKSPACE_INVALID` | 35 | The reviewer's working directory is not a clean checkout of the target. No reviewer ran. |
| `REVIEW_MALFORMED` | 31 | The output is not a valid verdict. |
| `REVIEW_SHA_MISMATCH` | 32 | The verdict describes a commit other than the target. |
| `TARGET_STALE` | 33 | The pull request moved while the reviewer was running. |
| `GITHUB_WRITE_FAILED` | 34 | The verdict was valid but the comment could not be created. |

Exit code `0` means *a validated review record for this exact target exists*,
whether this run created it or found it. Every failure keeps its own code:
"the reviewer broke" and "the pull request moved" call for different responses.

### Reviewer invocation

The reviewer is a command you configure, not a vendor integration:

```bash
review-loop review --pr 30 \
  --reviewer-command "/path/to/agent-cli -p --restricted-to-read-only-tools" \
  --dry-run
```

There is no default and no bundled reviewer: `--reviewer-command` names a
program you already have. Any command that reads a prompt on stdin and writes
a Structured Verdict to stdout qualifies — a coding-agent CLI in its
non-interactive mode is the obvious candidate, run with whatever flags make
it read-only and stop it waiting for a human. The first live experiment used
a locally installed coding-agent CLI in exactly that shape, authenticating
from its own existing login through `HOME`; see
[`docs/delegated-development/review-loop-live-experiment-1.md`](../../docs/delegated-development/review-loop-live-experiment-1.md)
for what it was configured with and what that did and did not guarantee.

* It is **tokenised with shell quoting rules but never run by a shell**, so
  `;`, `|` and `$(...)` are literal arguments to one program. Untrusted GitHub
  text — a title, a branch name, an author — is never part of the command line
  at all; it reaches the reviewer only inside the prompt, as data.
* The prompt arrives on **stdin**; the verdict is read from **stdout**. stderr
  is captured for diagnostics and never parsed, so a reviewer that writes a
  perfect verdict block to stderr has not produced one.
* The child gets an **environment allowlist** (`PATH`, `HOME`, `LANG`,
  `LC_ALL`, `TERM`, `TMPDIR`, `USER`), not this process's environment. A
  repository secret exported in your shell does not silently become the
  reviewer's to read. Pass what the reviewer genuinely needs with
  `--reviewer-env NAME`, repeatable.

**This is not a sandbox.** `--reviewer-command` names a *trusted, read-only
reviewer wrapper that you choose*. The reviewer runs as an ordinary child
process with your filesystem permissions, and `HOME` and `PATH` are on the
allowlist because a real reviewer needs them — which also means it can reach
`~/.config/gh`, `~/.ssh` and any tool on your path. A reviewer command that
decided to push, comment or merge could. "Read-only" here is an instruction in
the prompt plus a property of the command you configure; it is not a capability
boundary this tool enforces. Enforcing one — a sandboxed, credential-less
worktree — is a deliberate later change, not something this slice pretends to
have done.
* `--reviewer-timeout` (default 900s) abandons a reviewer that does not
  finish; stdout above 1 MB is refused rather than parsed.

No new credential is introduced: GitHub access is the existing `gh auth login`
session, and the reviewer's own authentication is whatever that command
already uses.

The prompt draws a line the rest of this tool depends on: the **review scope**
is the pull request's own change set — the diff GitHub shows, from where the
branch diverged to the head — while the CI base commit is **integration
context** only. They are not the same commit. `ci_merge_base_sha` is the
base-side commit CI merged the head onto, not necessarily the branch's fork
point, so once the base has advanced, diffing it directly against the head
would present base-only changes as though this pull request had made them.
The prompt says so explicitly, and tells the reviewer not to take that diff.

The prompt itself lives in `reviewer_prompt.py`, versioned with the code and
asserted by tests. It tells the reviewer to treat the pull request description
and any implementing agent's summary as **claims to be checked, not evidence**;
that it is read-only; and that repository content is review material, so
instructions found inside it are data, never orders to the reviewer.

### Where the reviewer runs

Binding the verdict to a SHA is worth little if the reviewer read a different
tree. The prompt names the target commit, so a reviewer echoes that SHA back
whether or not it ever looked at it — which means a reviewer pointed at the
wrong directory produces a verdict that passes SHA binding, passes validation,
passes revalidation, and is recorded as evidence about a commit nobody read.
Nothing downstream can catch that, because every downstream check reads the
same SHA out of the same prompt.

So the reviewer's working directory is now part of the review target:

* **By default the runner prepares one.** It fetches `refs/pull/N/head` from
  the remote, checks that the ref resolves to exactly the verified target SHA,
  creates a detached `git worktree` at that commit, runs the reviewer there,
  and removes the worktree afterwards. A fresh worktree holds only the
  commit's own files, so none of the leftovers described below can be in it —
  whatever the repository you invoked from happens to contain — including when the reviewer raises or
  the turn fails. The operator supplies nothing. They *cannot* usefully supply
  it: the target SHA is not known until verification has already run, so a
  directory chosen in advance is one chosen before anyone knows which commit
  is under review.
* **`--reviewer-cwd` replaces that with a directory you control** — a
  pre-warmed checkout, a container mount — and it is verified rather than
  trusted. It must be a git work tree, its `HEAD` must be exactly the target
  SHA, `git status --porcelain --untracked-files=all` must be empty, and
  `git ls-files --others --ignored --exclude-standard` must be empty too.

  All three kinds of leftover count, for the same reason and with different
  remedies. Uncommitted edits are code that is not in the pull request.
  Untracked files are files a reviewer can open and cite. And **git-ignored
  files are neither reported by `git status` nor invisible to a reviewer** —
  a `.env` sitting in a checkout is exactly as readable as any other file.
  That last one is why the check is two commands rather than one: this
  repository's `.gitignore` covers `.env`, `credentials.json`, `token.json`,
  `*.pem` and `*.key`, so on this path the files it catches are precisely the
  ones that must not reach a reviewer.

  This makes `--reviewer-cwd` strict — an everyday working checkout with a
  virtualenv or a real `.env` in it will be refused. That is the intended
  direction: the prepared worktree is the ergonomic path, and the override is
  for a workspace you have deliberately made clean.

Any failure is `REVIEWER_WORKSPACE_INVALID` (35), raised **before** the
reviewer starts. It is deliberately not `REVIEWER_FAILED`: nothing ran, and
the thing to fix is the workspace, not the reviewer.

`refs/pull/N/head` is fetched rather than the branch because the branch may
live in a fork this clone has no remote for, and that ref is what GitHub
resolved the head from. If it disagrees with the head the API reported, the
run stops — two authorities disagreeing about the target is exactly the case
where guessing is forbidden. `--git-remote` and `--repo-root` name the remote
and the repository to prepare from; they default to `origin` and the current
directory.

This is the first and only place the package writes to the **local**
filesystem: `git worktree add` and `git worktree remove` on a directory it
created under `TMPDIR`, plus the objects a `git fetch` brings in. The GitHub
write boundary is unchanged — one issue comment, still the only one.

**It is still not a sandbox.** Enforcing this invariant does not make it one,
and the distinction matters. The reviewer remains an ordinary child process
with your permissions; what changed is only that the tree it is *pointed at*
is now known to be the commit under review. Nothing stops it reading, or
writing, somewhere else entirely.

### Structured Verdict v1

The reviewer answers with one delimited block. Anything outside it is ignored
— a reviewer may reason out loud, and none of that reasoning is parsed or
recorded.

```text
BEGIN INDEPENDENT REVIEW VERDICT v1
Round: 1
Reviewed head SHA: 3b514700c1c2c257a39a7037f1a21ca5b9064106
Recommendation: changes_requested
Finding ID: F1
Severity: Major
Location: tools/review-loop/src/review_loop/runner.py:42
Problem: the head is re-read after evidence is collected, but the base is not
Evidence: run 33797660279 merged this head onto 6a2f7cf, which is no longer the tip
Required outcome: both ends of the tested merge are re-read before READY
Scope boundary: the runner and its tests; no change to the evaluator
END INDEPENDENT REVIEW VERDICT v1
```

The format is the `Label: value` style this repository's review comments
already use by hand, made explicit enough to parse without an LLM: a label
counts only at the start of a line, so a `Problem:` inside an indented code
snippet is text rather than a field boundary, and a paragraph-shaped field runs
until the next label. An unrecognised label at column 0 is an error, never
content — silently absorbing `Sevrity:` into the previous paragraph would turn
a typo into an invisible missing-field rejection.

Envelope: `Round`, `Reviewed head SHA`, `Recommendation`, optional `Resolved`
and `Escalation reason`. Per finding: `Finding ID`, `Severity`, `Location`,
`Problem`, `Evidence`, `Required outcome`, optional `Scope boundary`. Repeat
the finding group once per open finding; omit it entirely when there are none.

Every field except the two optional ones is required and must be non-empty.
**`Evidence` is required**: a finding without it is an assertion, and these
comments are recorded as evidence-bearing artifacts.

A verdict is rejected in full — no comment, no partial record — when:

* `Reviewed head SHA` is not **exactly** the target's 40-character SHA
* `Round` is anything but `1` (re-review is a later slice)
* `Recommendation` is unknown, or contradicts its own findings:
  `approved` with any finding, `changes_requested` with none, or `escalate`
  with neither a finding nor an `Escalation reason`
* a `Blocking` finding is paired with `changes_requested` — this project's
  standing decision is that Blocking always escalates to a human rather than
  being routed as a bounded fix
* a severity is not `Blocking`, `Major` or `Minor`
* a `Finding ID` is empty, repeated, or not a plain token
* any required field is missing or empty
* a field contains HTML-comment syntax or the marker prefix, which would let
  reviewer text forge the record's identity
* there are more than 50 findings, a field is over 4000 characters, or the
  rendered comment would exceed GitHub's limit

Recommendation and severity are matched case-insensitively (`Changes
Requested` → `changes_requested`). Nothing else is normalised, and nothing is
inferred: if the contract is not satisfied, the review is discarded rather
than interpreted.

### Why the exact SHA is the whole argument

A review is evidence about the commit the reviewer actually read. A verdict
naming an abbreviated SHA is not a vaguer way of naming the same commit — it
is a value this runner refuses to resolve, because resolving it is exactly the
guess that would let a review be recorded against a commit nobody reviewed.
Abbreviated, malformed, differently-cased and merely-different SHAs are all
one outcome: `REVIEW_SHA_MISMATCH`, zero writes.

### Post-review revalidation

After the reviewer returns and **before** anything is written, the full PR #28
verification runs again. All of the following must hold:

* the pull request still verifies as `READY`
* the head is still the exact SHA that was reviewed
* the base branch is still the same branch
* CI still validates that head merged onto **the same base commit** as when
  the review started

The last one is not implied by the others. The base can advance, CI can re-run
green against the new merge, and verification will report `READY` again — for
a merge context nobody reviewed. Any difference is `TARGET_STALE`, and nothing
is posted. The review may well have been correct about the commit it read; it
is simply not evidence about the pull request's current state, and this slice
does not record historical reviews.

### The recorded comment

```text
## Independent AI Review

Round: 1
Reviewed head SHA: 3b514700c1c2c257a39a7037f1a21ca5b9064106
CI integration base: master at 6a2f7cfe8cc8cb4af22b7824d1c70e6fce389bb8
CI verification: READY — .github/workflows/pytest.yml (run 33797660279: success)
Recommendation: changes_requested

Blocking: 0
Major: 1
Minor: 0
Open findings: 1

Findings:

### Major — F1

Finding ID: F1
Severity: Major
Location: tools/review-loop/src/review_loop/runner.py:42
Problem: ...
Evidence: ...
Required outcome: ...

---

Recorded automatically by `review-loop review`. ...

<!-- local-agent-concierge:independent-review:v1 repo=takolab/local-agent-concierge pr=29 head=3b51470... base=6a2f7cf... round=1 role=independent-reviewer -->
```

Only **validated fields** are rendered. The reviewer's raw output never reaches
GitHub, so prose, reasoning, or instruction-shaped text around the verdict
block cannot end up in the record. A review with nothing to report says
`Open findings: 0` explicitly.

### Identity and idempotency

A record is identified by its **hidden marker together with the comment's
author**, never by its heading. `## Independent AI Review` is a convention
this repository's humans already use by hand — PRs #26, #27 and #28 all carry
one written by a person — so treating the heading as proof of an automation
record would let a human comment suppress a real review. The marker carries
identity only: repository, pull request, exact head SHA, **the base commit CI
merged that head onto**, round, role. No secret, no prompt, no duplicate of
the verdict.

The author half matters because the marker is public and deterministic —
anyone who can comment on the pull request can reproduce it. A marker on its
own says *which* review a record would be, not *who* wrote it, so accepting
one from any author would let a copied string make this command report
`COMMENT_ALREADY_EXISTS` and exit `0` for a review that was never produced,
without even starting a reviewer. The runner resolves the account it would
post as (`gh api user`) and accepts a marker only from that account; a
matching marker under anyone else's name is ignored and the review proceeds
normally. If that account cannot be resolved, the run stops rather than
guessing.

This is a provenance check, not a signature. It does not defend against the
account itself — that is the same-identity residual risk below.

Identity is deliberately *not* "one review per pull request". A new head, a new
integration base, or a later round is a different record, so a future
re-review adds evidence rather than overwriting it.

The base commit is part of identity for the same reason the target is a merge
context rather than a commit. Without it, this happens: a review is recorded
for head `H` on base `B1`; `master` advances to `B2`; CI re-runs green for `H`
merged onto `B2`; the next run finds the old marker and reports
`COMMENT_ALREADY_EXISTS`. The tool would be claiming the current state is
reviewed, when the recorded review is evidence about a different integration
state. Post-review revalidation already refuses to conflate those two, so
identity has to agree with it — otherwise the duplicate check quietly
reintroduces the very stale-evidence case revalidation exists to prevent.

Retrying is safe. The duplicate check runs twice — once before the reviewer,
once immediately before the write — and the second is the one that matters
for the case where a `POST` succeeded but its response was lost: the retry
finds the marker and writes nothing.

### GitHub writes

Creating one issue comment is the only write **this package** performs. The
scope of that claim matters: it covers the Python code here, not the reviewer
subprocess, which runs with your own permissions as described above.

Within that scope the boundary is structural. PR #28's `github_client.py` remains read-only by
construction: it names no comment endpoint and issues no method but `GET`.
The single write lives in `github_comments.py`, in a class whose entire public
surface is one method, with `POST` and the `issues/{n}/comments` path
hard-coded. Tests assert at the source level that no other module in the
package contains a write method or a comment endpoint as a string literal, and
that the comment body travels as JSON on stdin rather than as a command-line
argument.

No code path in this package leads to editing or deleting a comment,
submitting a review object, changing a label, pushing, dispatching or
re-running a workflow, or merging.

`--dry-run` runs everything — including the reviewer and the revalidation —
and prints the comment it would have recorded. It never constructs a writer at
all, so there is nothing that could write even if the branching were wrong.

## Routing findings to a Coding Agent: `review-loop fix`

```bash
review-loop review --pr 29 --reviewer-command "my-reviewer" --json > review.json
review-loop fix --review-json review.json \
  --agent-command "my-coding-agent" \
  --write-patch fix.patch
```

```text
PR:                   #29 (base master)
Reviewed head SHA:    161669f40858aedfc4ebc6084338c990d2987870  [161669f]
Repository:           takolab/local-agent-concierge
Coding agent:         my-coding-agent
Agent workspace:      a detached worktree at the target, from /home/you/repo
Agent invoked:        Yes
Routed findings:      F1 (Major)
Allowed scope:        tools/review-loop/
Working tree:         tools/review-loop/src/review_loop/verdict.py, tools/review-loop/tests/test_verdict.py
HEAD after the run:   161669f40858aedfc4ebc6084338c990d2987870
Fix responses:
  F1: fixed (tools/review-loop/src/review_loop/verdict.py, tools/review-loop/tests/test_verdict.py)
Outcome:              FIX_APPLIED
Reason:
  - 1 open finding(s) are routable: F1
  - F1 was fixed in tools/review-loop/src/review_loop/verdict.py, ...
Patch:                fix.patch
GitHub write performed: No
Commit or push performed: No
```

The steps, in order, and why the order is the design:

1. **Load the routing input** and re-validate every field of it. See
   [The handoff](#the-handoff) — a document that would not have been an
   admissible verdict is not an admissible handoff.
2. **Gate, before preparing anything.** Whether the verdict may be routed at
   all is decided from the verdict alone, so an approval, a `Blocking`
   finding or an escalated review costs no fetch, no worktree and no child
   process. `--dry-run` stops here, and stopping here is what makes it a dry
   run rather than a rehearsal.
3. **Bind the workspace** — fetch `refs/pull/N/head`, check it resolves to
   exactly the reviewed commit, create a detached worktree there, verify it.
   PR #32's machinery, reused unchanged.
4. **Establish the change-set boundary** from git, then **resolve the allowed
   scope** inside it against that commit's own tree. A finding that cites no
   existing path, or only paths outside the boundary, is refused rather than
   routed with a guess or with borrowed authority.
5. **Run one Coding Agent turn** with the task contract on stdin.
6. **Inspect the working tree** — first, and on every path once the agent has
   started: a malformed response, and a failed or timed-out agent too. Reading
   the agent's answer before the tree would mean deciding what to look for
   based on what the agent said it did; and skipping the read when the
   *process* failed would discard the evidence in the case an operator most
   needs it. An agent that edited files and then timed out has still edited
   files, and with `--agent-cwd` those edits stay in a directory the runner
   does not remove.
7. **Validate the Structured Fix Response** against the routed identity and
   against that inspection.
8. **Capture the patch, then remove the worktree.** Always, on success and on
   every failure.

| Outcome | Exit | Meaning |
| --- | --- | --- |
| `FIX_APPLIED` | 0 | Every routed finding came back `fixed`, and the working tree agrees. |
| `NO_ACTIONABLE_FINDINGS` | 0 | The review asks for nothing. No agent ran. |
| `ROUTING_PREPARED` | 0 | `--dry-run`: the request was built and shown. No workspace, no agent. |
| `REVIEW_REQUIRES_HUMAN` | 40 | A `Blocking` finding, an escalated review, more findings than one turn admits, or a finding whose scope cannot be bounded. No agent ran. |
| `ROUTING_INPUT_INVALID` | 41 | The routing input is not a validated review. |
| `CODING_AGENT_WORKSPACE_INVALID` | 42 | The workspace is not a clean checkout of the reviewed commit. No agent ran. |
| `CODING_AGENT_FAILED` | 43 | The agent exited non-zero, timed out, or could not be run. |
| `FIX_RESPONSE_MALFORMED` | 44 | The output is not a valid fix response. |
| `FIX_TARGET_MISMATCH` | 45 | A response describes another commit. |
| `FIX_FINDING_MISMATCH` | 46 | The responses do not correspond one-to-one with the routed findings. |
| `FIX_SCOPE_VIOLATION` | 47 | The tree disagrees with the response, or holds a change the scope did not permit. |
| `FIX_NOT_APPLIED` | 48 | The agent could not fix a routed finding. |
| `FIX_ESCALATED` | 49 | The agent escalated a routed finding. |
| `PATCH_WRITE_FAILED` | 50 | The fix was valid but `--write-patch` failed. |
| `PATCH_TOO_LARGE` | 51 | The fix was valid but its diff was too large to capture, so no patch survives the run. |
| — | 2 | CLI usage error. |

Exit code `0` means *there is nothing left for this step to do* — either a
validated fix exists, or the review gave this step nothing to act on. Every
other value is a distinct reason, so a later slice can branch on why. The
codes occupy a block of their own: no fix outcome collides with a
verification verdict (0–20) or a review outcome (30–35), and a test asserts
that.

### The handoff

The fix turn's input is the review turn's own `--json` output, not a pull
request number. Two reasons, and one thing it deliberately is not.

Running the review again to recover its findings would pay for a second
reviewer and could produce a *different* verdict from the one a human read.
Passing the reviewed verdict forward fixes what was actually reviewed.

It is **not** a re-parse of untrusted text. Loosely scraping a GitHub comment,
or re-reading the reviewer's raw output, would mean deriving a verdict a
second time from something that was never a verdict. What happens instead is
that a machine-generated serialisation of the already-validated model is read
back through *the same invariants that produced it*: full 40-character SHAs,
the closed severity and recommendation vocabularies, the finding-id pattern,
the field limits, the round, and the review contract's own coherence rules
(`approved` with findings, `changes_requested` with a `Blocking` finding, a
verdict whose reviewed SHA is not its target's). A handoff that would not have
been an admissible verdict is refused.

A handoff file is operator-controlled input, and that is not a new authority:
someone who can write it could equally run the command with different
arguments. What they cannot do is route a fix against a commit that is not the
pull request's head, because the workspace resolves `refs/pull/N/head` and
refuses anything else. **Git, not the file, decides which commit gets fixed.**

It is also not persistent state. It is one file piped from one command into
the next; nothing reads it later, nothing accumulates, and deleting it loses
nothing GitHub does not already hold.

### Which findings route

| Verdict | What happens |
| --- | --- |
| `approved`, or no open findings | `NO_ACTIONABLE_FINDINGS`. No agent runs. |
| Any `Blocking` finding | `REVIEW_REQUIRES_HUMAN`. This project's standing decision: a Blocking finding goes to a human and never into an automated fix. |
| `escalate` | `REVIEW_REQUIRES_HUMAN`, with the reviewer's escalation reason. |
| More than `--max-findings` (default 5) open findings | `REVIEW_REQUIRES_HUMAN`. A turn carrying twenty findings is not bounded in any useful sense. |
| Otherwise | All open findings route, in **one** agent turn. |

The rule is re-applied here rather than assumed from the review side, because
the verdict travelled through a file to get here, and a rule checked only
upstream holds only as long as nothing changes upstream.

**One verdict, one turn, one response block per finding.** Several findings
share a turn, and each gets its own `Finding ID` block with its own outcome,
so identity stays singular even when the turn is not. There is no parallel
multi-agent routing here and no plan for one at this scale.

### The Coding Agent's workspace

The reviewer's workspace and the agent's are the same at the start and
deliberately different at the end.

**Before the agent runs**, PR #32's rules apply verbatim, via the same code:
the directory is a git work tree, its `HEAD` is exactly the reviewed commit,
`git status --porcelain --untracked-files=all` is empty, and
`git ls-files --others --ignored --exclude-standard` is empty too. A writable
workspace that already contains someone else's edits, or a stray `.env`, is
not a workspace whose *final* state means anything. Any failure is
`CODING_AGENT_WORKSPACE_INVALID` (42), raised before the agent starts.

By default the runner prepares that workspace itself — fetch
`refs/pull/N/head`, require it to resolve to exactly the reviewed commit,
`git worktree add --detach`, verify, run, remove. `--agent-cwd` replaces it
with a directory you control, verified the same way; its contents *will* be
modified, so it should be a directory you dedicated to this.

**After the agent runs**, three kinds of change are distinguished, because
they are three different facts:

* **Tracked and untracked changes are the fix.** They are compared, as a set,
  against what the agent said it changed and against the routed scope.
* **Build and test residue is expected.** `__pycache__`, `.pytest_cache`,
  `.hypothesis`, `.venv`, `node_modules`, `*.pyc` and the like are git-ignored,
  are therefore not part of the fix and cannot reach a pull request, and are
  reported and tolerated. Reusing the reviewer's zero-ignored-files rule here
  would fail every turn in which the agent did what it was told and ran the
  tests.
* **Any other git-ignored path is neither.** The tree started with none, so
  each was produced by this run, and this repository's `.gitignore` covers
  `.env`, `credentials.json`, `token.json`, `*.pem` and `*.key`. A run that
  ends with one of those is `FIX_SCOPE_VIOLATION`.

**The agent must not commit.** `HEAD` is re-read afterwards and a moved one
fails the run: a committed fix is a change `git status` no longer reports,
which is exactly where a hidden change would hide. Committing, and everything
after it, is the next slice's decision — with a human in it.

The worktree is removed on every path, so **use `--write-patch`**: without it
the fix is reported and then discarded with the directory it lived in. For the
same reason a diff too large to capture (`MAX_PATCH_BYTES`) is not a success —
it is `PATCH_TOO_LARGE` (51), never exit 0, because the run would otherwise
announce a fix and then throw it away. `--agent-cwd` is the exception to the
removal: that directory is yours, is not cleaned up, and keeps whatever the
agent left in it, including after a failed run.

### The allowed scope

A reviewer writes `Location` for a human, so it is prose. Deriving an exact
permitted file set from prose is not possible, and pretending otherwise would
produce a check that fails on correct fixes and passes on incorrect ones.

The scope is therefore built from **two** sources, and which one is the
*authority* matters more than the arithmetic.

**1. The pull request's own change set is the outer boundary.** Taken from
git — the paths this branch changed relative to the point it diverged from its
base — so neither the reviewer nor the agent has any influence over it. Each
changed path contributes its **component root**:

> the nearest ancestor directory holding a build manifest (`pyproject.toml`,
> `package.json`, `go.mod`, `Cargo.toml`) — **never the repository root**;
> failing that the path's own directory, and for a repository-root file, the
> file itself.

It is computed the way the reviewer prompt tells a reviewer to compute the
change set, against the divergence point rather than against `ci_merge_base_sha`.
Those differ once the base branch advances, and using the second would widen
the boundary with commits nobody in this pull request wrote.

**2. A finding's `Location` selects within that boundary.** Its cited paths
contribute their own component roots, and a component root outside the
boundary is **discarded rather than granted** — recorded and printed as
`Cited but out of PR`, so an operator can see what the reviewer was pointing
at.

The direction of that second rule is the whole point, and it is a correction:
the first version of this slice derived the scope from `Location` alone, which
made reviewer-written text an authority over what the agent could edit. A
finding naming a component the pull request had never touched would have been
granted write access to it, with the prompt-injection boundary applied *after*
the scope had already been computed from the same untrusted text. Independent
review of PR #34 found that; the finding was correct. **Reviewer prose can now
narrow the scope; it cannot widen it.**

For a finding at `tools/review-loop/src/review_loop/verdict.py` in a pull
request that changed that package, the scope is `tools/review-loop/`: the agent
may edit its source, its tests and its README, and may not touch
`services/orchestrator/` or `.github/workflows/`. "Primary location + related
tests + necessary docs, inside what this pull request already touches" — wider
than the cited file on purpose, because a fix whose test cannot be updated is
not a fix.

Cited paths are untrusted text, so an absolute path, a `..` component, or a
symlink resolving outside the worktree contributes nothing.

Three ways this fails closed, none of them with a guess:

| Situation | Outcome |
| --- | --- |
| The base branch is not available locally and cannot be fetched, so the change set is unknown | `CODING_AGENT_WORKSPACE_INVALID` (42). No agent runs — a scope with no authority behind it is not a scope. |
| A finding cites no path that exists at the reviewed commit | `REVIEW_REQUIRES_HUMAN` (40) |
| Every path a finding cites lies outside the change set | `REVIEW_REQUIRES_HUMAN` (40) |

`--allow-path PATH` is the deliberate escape hatch, and the **only** input that
may reach beyond the change-set boundary — because it comes from an operator
who has read the finding, which is exactly the human authorization the boundary
exists to require. It is repeatable, appears in the agent's task contract and
in the output, and may name a file that does not exist yet.

### Structured Fix Response v1

The agent answers with one delimited block **per routed finding**:

```text
BEGIN BOUNDED FIX RESPONSE v1
Finding ID: F1
Target head SHA: 161669f40858aedfc4ebc6084338c990d2987870
Outcome: fixed
Files changed:
- calc/calc.py
- calc/tests/test_mean.py
Verification: python -m pytest calc/tests: 4 passed
Summary: mean() now rejects an empty sequence with a ValueError naming the
  input, so the required outcome holds. divide() is untouched.
Scope notes: only calc/ was touched.
END BOUNDED FIX RESPONSE v1
```

Same parsing rules as the Structured Verdict, for the same reasons: only
delimited text is read, a label counts only at column 0, and an unrecognised
label-shaped line at column 0 is an error rather than content. One rule
differs — a fix turn produces a *sequence* of blocks, and an empty sequence is
a failure rather than an empty answer.

| Field | Required | Notes |
| --- | --- | --- |
| `Finding ID` | always | Must be one of the routed ids, answered exactly once. |
| `Target head SHA` | always | Exactly the 40 characters of the reviewed commit. |
| `Outcome` | always | `fixed`, `unable_to_fix`, or `escalate`. Nothing else. |
| `Files changed` | always | `- path` per line, or `(none)`. A bare path with no marker is refused: it is indistinguishable from a wrapped continuation. |
| `Summary` | always | |
| `Verification` | when `fixed` | A fix reported with no verification is an assertion. |
| `Reason` | when not `fixed` | What stopped the fix, or what is being escalated. |
| `Scope notes` | optional | Where an agent reports what it deliberately did not touch. |

Two rules are worth naming:

* **`fixed` requires at least one changed file.** There is no "fixed, no code
  change". If a finding turns out to need no change, that is not a fix — it is
  a disagreement with the reviewer, and the contract has a word for it:
  `escalate`.
* **`unable_to_fix` and `escalate` must leave nothing behind.** Half-finished
  edits with no claim attached are the worst possible artifact to hand a
  human: they look like a fix and are not one.

The turn's single outcome aggregates the blocks: `FIX_APPLIED` only if every
one is `fixed`; any `escalate` makes the turn `FIX_ESCALATED`, which outranks
everything else, because a human has been asked a question and a green exit
code would bury it.

### What is checked, and what cannot be

The response is checked, never believed.

**Identity.** Each block names exactly one routed finding and exactly the
commit the fix started from. Abbreviated SHAs are refused rather than
resolved. The set of blocks must correspond one-to-one with the routed
findings — a silently dropped finding looks exactly like a handled one.

**The working tree.** The union of every block's `Files changed` must equal
what `git status --porcelain -z --untracked-files=all` reports. A file changed
but not reported fails the turn; so does a file reported but not changed. This
is the check that makes `Files changed` a claim rather than a courtesy. Every
changed path must also fall inside the routed scope.

**What is not checked:** whether the fix is *correct*. That cannot be
mechanised here — the reviewer's `Required outcome` is prose, and a runner
grading prose would be a second reviewer with none of the first one's
independence. Nor is the agent's reported `Verification` command re-run: that
string comes from the agent, and executing it would hand an untrusted process
exactly the arbitrary-command channel the rest of this design refuses it. So
the runner establishes that the fix is *the one that was asked for, in the
place it was allowed, and no more*. Whether it is right, and whether it is
used at all, stay with the human.

### Coding Agent invocation

The agent is a command you configure, run through the same mechanism as the
reviewer (`bounded_process.py`), which is where the security argument for both
roles lives in one place:

* **no shell** — the command is tokenised into an argument vector; untrusted
  text never reaches a command line;
* **an allowlisted environment** — `PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`,
  `TMPDIR`, `USER`, plus whatever `--agent-env NAME` names. No credential
  variable is on that list. A coding agent legitimately needs more than a
  reviewer, and it gets exactly what is named;
* **a timeout** (default 1800s, longer than the reviewer's, because making a
  change and running its tests is not the same work as reading) and an
  **output size limit**;
* **stdout only** — a response block written to stderr has not been produced.

### GitHub and credential boundary

`review-loop fix` makes **no GitHub request of any kind** — not a read, not a
write. It constructs no client, reader or writer; the subcommand dispatch
threads none in. The one fact a fix turn needs from outside — that the commit
it is fixing is still this pull request's head — comes from git resolving
`refs/pull/N/head`, which is the same fact a GitHub round-trip would have
established, obtained without a credential.

That claim is asserted at the source level, the way PR #29's single-write
boundary is: tests parse each of the ten fix-path modules and assert that none
imports `github_client` or `github_comments`, names `gh`, names a write HTTP
method, or contains a GitHub API host.

**No new credential is introduced.** The agent needs no GitHub token, and
`GH_TOKEN`, `GITHUB_TOKEN` and every other credential variable are off the
default allowlist. Nothing here pushes, commits, comments, labels, merges or
dispatches a workflow.

### Prompt injection boundary

A coding agent has two untrusted inputs where a reviewer has one: repository
content, and the reviewer's finding text. The task contract states both
explicitly — repository files may contain instructions and are material, the
finding text is a reviewer's claim quoted verbatim, and only the
runner-generated contract defines authority. Nothing read from either can
widen the allowed scope or authorise an external write.

This is stated **and** backstopped, in that order of reliability. A finding
that talked an agent into claiming a fix it did not make still fails, because
the claim is checked against the diff; one that talked it into editing
elsewhere still fails, at the scope check. As a smaller measure, a finding
whose own text contains the fix-response delimiters is refused before routing:
reviewer text may not contain the marker its own answer is read from.

The load-bearing part is structural rather than textual: **the finding text is
not an authority over the scope**. It selects within the change-set boundary
and cannot extend it, so the worst a hostile or mistaken finding can do is
narrow the fix or fail to route — never widen what the agent may write to. An
earlier version of this slice did not have that property, and the boundary
exists because independent review found it missing.

### What is structurally enforced, and what is only asked for

Worth separating, because a table of instructions can read like a table of
guarantees.

| | |
| --- | --- |
| **Enforced by this runner** | The workspace is the reviewed commit and starts clean. The scope's outer limit comes from git, not from reviewer text. The response names the routed finding and the exact commit. The reported file set equals the actual one. Every changed path is inside the routed scope. `HEAD` did not move. No unexpected git-ignored file was left. A fix that cannot be handed back does not exit zero. The runner itself makes no GitHub request and creates no commit. |
| **Asked for in the prompt only** | That the agent does not commit or push *itself* (detected afterwards, not prevented). That it does not touch GitHub. That it does not read credentials. That it does not work outside the worktree. |

**It is not a sandbox.** The agent is an ordinary child process running as the
invoking user. A target-bound worktree is where it is *pointed*, not a wall
around it: it can read and write elsewhere on the filesystem, and if the
machine has a usable `gh` login it could use it. That is exactly why the
working tree is inspected independently rather than trusted — but the
inspection establishes what happened *inside the worktree*, and an agent that
wrote somewhere else did so as you, unobserved.

## Tests

```bash
pip install -e "tools/review-loop[test]"
python -m pytest tools/review-loop/tests
```

No test performs network access, invokes a real reviewer or coding agent, or
requires credentials; the GitHub API is replaced by fakes built from real
recorded response shapes, and the reviewer- and agent-process tests run
`sys.executable` with an inline script. CI needs no agent credentials.

The workspace tests do drive **real `git`**, against repositories built in a
temporary directory — the "remote" is a bare repository on disk and the head
ref is pushed into it as `refs/pull/N/head`, the way GitHub exposes it. A
faked git would prove nothing here: the failure being prevented is a claim
about what git actually checked out, so the assertions are about real
detached worktrees and a really dirty working tree.

The fix-turn tests extend that to what an agent *leaves behind*. A scripted
agent really edits files in a really prepared worktree, and the assertions are
about what `git status` then reports: a hidden extra change, an out-of-scope
edit, a real `git commit` made inside the worktree, a real `.env` left in it,
and a real `__pycache__` that must not fail the turn. The operator's own
checkout is asserted unchanged, and the worktree asserted removed, on the
success path and on the failure paths alike.

## Known limitations

* **Scope is coarse, and can refuse legitimate findings.** A reviewer that
  describes a location without naming a path, or that names only paths outside
  the pull request's change set, gets `REVIEW_REQUIRES_HUMAN` rather than a
  guess. That is the intended direction, but it means a legitimate finding —
  "your change here breaks the caller over there" — does not route without an
  explicit `--allow-path`. A component root is also wider than most fixes
  need: within `tools/review-loop/` the scope check would not catch an
  unrelated edit to a neighbouring file in the same package.
* **The change-set boundary needs the base branch.** It is resolved from a
  remote-tracking ref, a local branch, or a fetch, in that order. A clone
  without the base branch and without network access cannot establish it, and
  the run fails closed at `CODING_AGENT_WORKSPACE_INVALID` rather than falling
  back to reviewer-derived scope.
* **The fix is checked for shape and place, never for correctness.** The
  runner establishes that the change is the one that was asked for, where it
  was allowed, and no more. Whether it actually satisfies the reviewer's
  `Required outcome` is not mechanised, and the agent's reported
  `Verification` is a claim that is deliberately not re-run.
* **A fix turn is not sandboxed.** See
  [What is structurally enforced](#what-is-structurally-enforced-and-what-is-only-asked-for).
  The worktree binds where the agent is pointed, not what it can reach.
* **The fix exists only as a patch.** Nothing commits, pushes, or updates the
  pull request; without `--write-patch` the change is discarded with the
  worktree. Applying it is the human's, and so is everything after.
* **Only the initial review round.** `Round: 1` is the only accepted value;
  re-review, finding-resolution tracking across rounds, and the multi-round
  loop are not implemented. The record identity already includes the round and
  head SHA so that they can be added without overwriting existing evidence.
* **A residual race on the write.** GitHub offers no compare-and-set on issue
  comments. The window between the final duplicate check and the `POST` is
  narrow but real; two runners racing on the same target could produce two
  comments.
* **One GitHub identity for every role.** The reviewer record is posted under
  the same account that authors the pull requests, so the role is a convention
  rather than an access-controlled fact. The duplicate check verifies that a
  record came from that account, which separates this automation's records
  from everyone else's comments — but nothing separates the roles *within*
  that one account, so it cannot tell a genuine record from one the account
  wrote by hand. Accepted deliberately at this project's scale; a human merge
  decision remains the backstop.
* **The reviewer is still trusted, in two ways — one of them now narrower.**
  It is trusted to have actually reviewed: this runner validates the *shape*
  and *binding* of a verdict, not its truth, so a reviewer that invents
  findings still produces a well-formed record. What it can no longer do
  silently is review the wrong tree — the working directory is verified to be
  the target commit before it starts. That is a guarantee about the directory
  the reviewer is given, not about what it reads: a reviewer is free to open
  files elsewhere on the filesystem, and nothing here would notice.
  And it is still trusted not to write: it runs as an ordinary child process
  with the invoking user's permissions and can reach `~/.config/gh` and
  `~/.ssh` through `HOME`. The structural write guarantee covers this package,
  not the command you point it at. A credential-less, sandboxed reviewer is
  still a later change.
* **No persistent state.** Everything is reconstructed from GitHub and the
  current invocation on each run; there is no database and no daemon.
* **Historical reviews are discarded.** If the target moves during the review,
  the result is dropped rather than recorded as evidence about the older
  commit.

## Scope boundary

This slice ends at "one validated review recorded against one verified pull
request state, and one bounded local fix routed from it". Out of scope here,
and left for later slices: committing the fix, pushing it, updating the pull
request, waiting for CI after a fix, Independent Re-Review, multi-round loops,
finding resolution tracking across commits, the Merge Decision Brief,
automatic merge, any server or daemon, and any persistent state.

That live trial has now happened, on PR #30, and is recorded in
[`docs/delegated-development/review-loop-live-experiment-1.md`](../../docs/delegated-development/review-loop-live-experiment-1.md).
It concluded that the flow does replace starting a review by hand, and named
the unbound reviewer working directory as the one thing to fix first — which
is what "Where the reviewer runs" above now does. That fix was then validated
live, against a pull request in a different repository, in
[`docs/delegated-development/review-loop-live-experiment-2.md`](../../docs/delegated-development/review-loop-live-experiment-2.md).

Structured Findings → Coding Agent routing + Bounded Fix Response is what
`review-loop fix` above now does.

The next slice is **bounded fix → exact fix commit identity → push → wait for
authoritative CI**, followed later by fresh-context Independent Re-Review,
finding resolution, the multi-round loop and the Merge Decision Brief. None of
that is here, and the human gate is why: this pipeline automates *routing and
bounded local fixing*. It does not automate acceptance.
