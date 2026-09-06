# Review loop runner

A local command that runs one Independent AI Review turn against a pull
request, records the result only when it can prove which exact pull request
state that review describes, routes the findings that review produced to one
bounded Coding Agent turn against that same exact state, and commits and
pushes the patch that produces — proving, at each step, exactly which change
is being carried forward.

It has five commands, and only the fourth can change the repository.

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

**`review-loop push --fix-json <file> --patch <file>`** is the first stage
with **authoritative repository write capability**. It proves the patch it
holds is the one the fix turn validated, commits it on the exact reviewed
head, fast-forward pushes that commit to the pull request's own branch, reads
the remote ref back to confirm the exact pushed SHA, and waits for
authoritative CI on that exact commit. Its entire write surface is one
`git push` to one derived ref; GitHub itself stays read-only.

**`review-loop re-review --review-json <file> --push-json <file>`** runs one
*fresh* Independent Re-Review against that pushed fix, and only if the pull
request is still at it with authoritative CI green against the current merge
context. A new reviewer process, with no access to the Coding Agent's
context, reads that exact commit and answers two separate questions: did each
original finding get resolved, and what does a fresh review of the pull
request as it now stands find? Both answers are recorded, separately, as one
`## Independent AI Re-Review` comment.

Everything after that — a second fix round, the multi-round loop, the Merge
Decision Brief, merge — is not here. **The full Finding → Fix → Re-Review
loop is not automated.** This is one review turn, one bounded fix turn, one
commit-and-push turn and one re-review turn, each bound to one verified
state, with the human keeping every decision about acceptance and merge.

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

### What a fix turn guarantees, and what it does not

Worth stating as a contract, because the boundary is deliberately narrower
than it first looks:

> `review-loop fix` verifies that the pull request head still equals the
> reviewed head SHA. It does **not** re-establish the review's original
> merge/CI context. The produced patch is a **candidate fix against the
> reviewed head**, not evidence that the current pull request merge context is
> valid. Current authoritative CI and a fresh Independent Re-Review are
> required before merge.

So this is the whole guarantee, and each arrow is mechanical:

```text
validated review finding
→ exact reviewed head H is still the pull request head
→ one bounded Coding Agent turn against H
→ machine-checked candidate patch for that reviewed head
```

**Head currency is the structural gate; merge-context currency is not.** If
the base advances from B1 to B2 while the head stays at H, a fix turn still
runs against H, and that is correct rather than a gap:

```text
review: B1 + H
base advances: B1 → B2
fix: still allowed against the exact reviewed head H
```

Nothing here commits, pushes, merges or deploys, so the artifact is a patch
proposed against a commit that demonstrably still is the pull request's head.
The stronger current-state guarantees belong to the stages that actually act:

```text
candidate patch
→ commit / push
→ authoritative CI against the current merge context
→ fresh Independent Re-Review
→ human merge decision
→ merge
```

The first two of those are now
[`review-loop push`](#committing-and-pushing-the-fix-review-loop-push), which
does re-establish merge-context currency before reporting `PUSH_READY` —
because unlike a fix turn, it writes.

One property makes this coherent rather than merely convenient: **the change-set
boundary does not drift when the base moves.** `merge-base` backs up to where
the branch diverged, so a base that has advanced since contributes nothing to
what the agent may edit. The boundary describes the pull request, not the
freshness of its merge context, and a test pins that
(`test_a_base_that_advanced_after_the_review_does_not_change_the_change_set`).

The review turn does check merge-context currency, before and after the
reviewer runs — see [Post-review revalidation](#post-review-revalidation).
That check belongs there, because a review is a *record* about a merge context;
a candidate patch is not.

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

**The base tip is fetched every turn**, and a local `origin/<base>` is
deliberately *not* consulted. That ref is a cache with no expiry — a fix turn
fetches `refs/pull/N/head` and nothing else, so in a clone made before the base
advanced it can be arbitrarily old. The failure is concrete: with
`origin/master` at B0, a base that has since advanced to B1 changing an
unrelated component, and a pull request branched from B1, `merge-base(B0, H)`
is B0 and `diff B0..H` reports B1's component as part of this pull request. It
would enter the boundary, a finding's `Location` could select it, and reviewer
prose would regain write scope over a component the pull request never touched.
It is the same mistake as using `ci_merge_base_sha`, reached by a different
route: both substitute a remembered base for the current one. (Re-review of
PR #34 found this; the finding was correct.)

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
| The base branch cannot be fetched from `--git-remote`, so the current base tip is unknown | `CODING_AGENT_WORKSPACE_INVALID` (42). No agent runs, and a stale local ref is never used instead — a scope with no authority behind it is not a scope. |
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

## Committing and pushing the fix: `review-loop push`

**This is the first stage of the review loop with authoritative repository
write capability.** Everything before it produced records and files; this one
puts a commit on a branch that GitHub builds and a human merges. That is the
whole reason its correctness argument is stated at this length.

```bash
review-loop fix --review-json review.json \
  --agent-command "..." --write-patch fix.patch --json > fix.json

review-loop push --fix-json fix.json --patch fix.patch
```

What it does, in the order it does it:

```text
validated candidate patch
→ exact patch identity verification
→ exact fix commit on the reviewed head
→ fast-forward push to the pull request's own branch
→ exact pushed commit SHA, read back from the remote
→ authoritative CI for that exact commit
→ CI classification
```

### The write boundary

Exactly one repository mutation is possible:

> A **fast-forward `git push` of one commit to `refs/heads/<the pull request's
> head branch>`**, on the remote named by `--git-remote` (default `origin`).

Everything that is *not* possible is worth listing, because the list is the
design:

| | |
| --- | --- |
| **Can** | Fast-forward one commit onto the pull request's own head branch, in this repository. |
| **Cannot** | Force push, or lease against the local remote-tracking ref. Write a tag — including one carried along by `push.followTags`. Push submodule commits to their own remotes. Create a branch that does not exist. Push to the default branch, the base branch, or a fork's branch. Push to a remote that names a different repository. Push an arbitrary refspec. Rebase, reset, cherry-pick, merge, or rewrite history. Merge the pull request. Write anything at all to the GitHub API. |

**What that guarantee covers, precisely.** It is a statement about *the git
argument vectors this runner constructs* — enumerated by a test that walks the
AST and asserts the complete set of subcommands. It is **not** a statement
that no other write can occur while the command runs, because `git commit` and
`git push` deliberately run your normal hooks, and a `pre-commit`,
`prepare-commit-msg` or `pre-push` hook is an arbitrary program that can write
files, reach the network, or touch other repositories. Hooks are not bypassed
— `--no-verify` is not passed — because they are your configuration on your
machine and a runner that gained push authority this week is not the thing
that should start ignoring them. The trust assumption is therefore explicit:
**your configured hooks are trusted; the runner's own argv is bounded.** A
hook that modifies files is still caught, because the commit is re-hashed
against the candidate patch afterwards.

The branch is not a parameter. **There is no `--branch`, no `--ref`, no
`--refspec` and no `--force`**, and a test asserts that the parser offers
none of them — because the flag that would undo this design is the one that
lets an operator, or a script quoting agent output, name the ref. The refspec
is built inside [`push_branch.py`](src/review_loop/push_branch.py) from
GitHub's own pull request object and from nothing else.

**The repository is checked too, not just the ref name.** A ref name is half a
destination; `--git-remote` supplies the other half, and it *is* operator
controlled. So **every** URL git reports for that remote must name the
repository the validated handoff describes — read with `--all`, in both
directions:

```bash
git remote get-url --all <remote>
git remote get-url --push --all <remote>
```

`--all` is not thoroughness for its own sake. A remote may have several push
URLs and `git push` writes to **every** one of them, while
`git remote get-url --push` without `--all` reports only the first. Checking
that first URL while pushing to all of them is not a check; it is the
appearance of one.

The rule each URL must satisfy:

* a URL with a **host** must be a GitHub host, and its `owner/name` must be
  the target repository. `--git-remote upstream` pointing at `someone/fork`,
  or at another forge entirely, is refused;
* a URL with **no host** — a local path — is accepted when its final two
  segments name the target repository. This runner cannot prove a local path
  is GitHub and says so rather than pretending; an operator who constructs a
  path ending in `<owner>/<name>.git` to redirect this push can already push
  there by hand.

`git remote get-url` reports the *effective* URL, with any
`url.<base>.insteadOf` rewriting already applied, so a rewrite cannot hide
behind this check.

**Credentials.** The push travels over your existing git credential for the
remote — an SSH key or a credential helper. No new token is introduced, and
none is read from the environment by this tool. GitHub itself stays read-only
here: the same `gh api --method GET` client the verification command uses. No
comment, label, review, dispatch or merge is written, and a source-level test
asserts that no push-path module imports the comment writer or names a write
HTTP method.

**Your git identity authors the commit.** `git commit` runs with your
environment, so `user.name` and `user.email` must be configured, and the fix
commit is authored as you. That is deliberate rather than incidental: a
machine-generated commit landing under a human's name is a fact worth being
visible in `git log`, not one to paper over with a synthetic identity. A
missing identity, or a signing configuration that cannot sign, is
`COMMIT_REFUSED` with git's own message — reported, never bypassed.

**One repository, one authority.** The GitHub client is constructed from
`handoff.target.repo` — never from `detect_repository()`, which would read the
repository out of whatever clone you happen to be standing in and give the
GitHub side of this command a different authority from the git side. `--repo`
is an *assertion* that the handoff describes that repository, not a selector.
A test asserts the module does not even import the directory-based detector.

For the same reason the pull request must be a pull request **in** that
repository at both ends: `head.repo.full_name` and `base.repo.full_name` must
both equal it. Checking only the head would admit a cross-repository pull
request in someone else's repository whose head happens to live here, and the
branch named in one of those is not a branch this fix may be pushed to.

**Who controls what.**

| Input | Controlled by | Treated as |
| --- | --- | --- |
| `--fix-json` document | the operator | a *selection*, re-validated field by field; it cannot name a commit that is not the head, or a patch whose bytes disagree with its digest |
| the patch file | the operator | checked against the digest before anything is applied |
| the pull request object | GitHub | **authoritative** for the branch, the base, the default branch and the head SHA |
| `--git-remote` | the operator | a *selection*, checked against `target.repo` before any write |
| the remote's URLs | git | **authoritative** for which repository a push would reach |
| the remote branch tip | the remote | **authoritative** for what is already pushed |
| the created commit's parent and diff | local git | **authoritative** for what was committed |
| reviewer finding text, Coding Agent output | untrusted | reaches neither the refspec nor the commit message |

### Exact candidate patch identity

The fix turn records a SHA-256 of the diff it captured. The push turn checks
that digest **three times**, against three different things, and the third is
the one that matters:

1. **The file.** Its bytes must hash to the recorded digest. A patch from
   another fix turn, or one edited by hand, stops here — before `git apply`.
2. **The applied tree.** After `git apply --index`, the working tree is
   re-diffed and must hash to the same digest. This is what makes "no
   unrelated change leaked in" a checked fact: any extra edit, from any
   source, changes the diff and therefore the digest.
3. **The commit.** Once the commit exists, *git's own account of it* —
   `diff <parent> <commit>` — must hash to the same digest again, its parent
   must be the reviewed head, it must be exactly one commit, and the working
   tree must be clean afterwards.

A digest is only comparable if a diff is a function of its content, which by
default it is not. `index` lines abbreviate to a length derived from the
repository's object count; `diff.noprefix`, `diff.algorithm`, `diff.context`,
`diff.indentHeuristic`, `diff.orderFile`, `mnemonicPrefix` and textconv
drivers are all ordinary user configuration; `core.quotePath` decides whether
a non-ASCII path is rendered literally or octal-escaped; and **rename
detection** renders one tree transition either as `rename from`/`rename to` or
as a delete plus an add, depending on `diff.renames` and — when renames are on
— on `diff.renameLimit`, and therefore on how many files the change happened
to touch.

So every load-bearing diff goes through one canonical argument vector in
[`patch_identity.py`](src/review_loop/patch_identity.py), which pins each of
those. Tests cover it from three directions: hostile local `git config` for
every pinned setting does not move the digest, a non-ASCII path and a rename
each hash identically under both settings, and the same change hashes
identically in a second clone with a different object count.

The claim that comes with that is deliberately bounded: the digest is a
property of the change **for the settings this module pins**, which is what
makes two runs on two machines comparable. It is not a claim that no git
configuration anywhere can affect it — `core.fileMode`, for instance, changes
what git *sees* in a working tree rather than how a diff is rendered, and is
not pinned because forcing it would break clones on filesystems that need it
off.

**The patch path is resolved before any worktree exists.** The file is read
from your working directory and `git apply` runs inside a temporary worktree,
so a relative `--patch fix.patch` — exactly what the documented flow produces
— would otherwise pass the identity check and then fail to open. The resolved
absolute path is used for both and reported in the result. For the same
reason, `review-loop fix --write-patch` writes the patch in **binary** mode:
the identity is the captured UTF-8 bytes, and text-mode newline translation
would put different bytes on disk from the ones the digest describes.

### Exact reviewed-head ancestry, and the branch this may reach

Before anything is applied, four facts are re-established from their own
authorities:

* **The pull request object** (GitHub) — still open, still this number, head
  branch in *this* repository, head branch that is neither the base nor the
  default branch, base branch unchanged since the fix turn, and an exact
  40-character head SHA.
* **The remote branch tip** (`git ls-remote`) — what `refs/heads/<branch>`
  actually points at right now.
* **The workspace** — a detached worktree at the exact reviewed head, clean,
  with no git-ignored residue. `--commit-cwd` can supply your own directory
  instead; it is *verified*, not trusted.
* **The patch** — applies to that head, producing exactly the validated change.

If the head has moved, the run stops with `PUSH_TARGET_STALE` and nothing is
written. **The patch is never rebased or adapted onto a different head.** A
fix for a commit is a fix for that commit; making it apply somewhere else is a
new fix turn's job, with a human deciding to run it.

**The write is a compare-and-swap.** Reading the branch and then pushing are
two operations, and between them the branch can move. Two ways that matters,
both of which a plain `git push` gets wrong:

* **deleted** in between — a plain push *recreates* it, contradicting "never
  creates a branch that does not exist";
* **rewound to an ancestor** in between — `A → C` is still a fast-forward, so
  a plain push writes over a ref that is no longer at the state the fix was
  authorised against.

So the push carries an explicit lease on the exact expected old value:

```bash
git push --porcelain \
  --no-follow-tags --recurse-submodules=no \
  --force-with-lease=refs/heads/<branch>:<reviewed head> \
  -- <remote> <commit>:refs/heads/<branch>
```

**The two `--no-*` flags are not decoration.** An explicit refspec bounds what
this runner *asks* for; it does not bound what the operator's configuration
adds to the request. `push.followTags=true` pushes an annotated tag reachable
from the commit — a second ref update, in the one namespace this command
promises never to write — and `push.recurseSubmodules=on-demand` pushes
submodule commits to *their* remotes, turning one repository write into
writes to several. Both are refused explicitly.

Two neighbouring settings were checked and need no flag: a configured
`remote.<name>.push` refspec is overridden by the one on the command line, and
`remote.<name>.mirror` makes git refuse outright rather than expand
(`--mirror can't be combined with refspecs`).

And because "the flags are right" is a claim about this argv rather than about
what happened, **the remote's own report is checked for refs nobody asked
for** — and classified by what the report actually establishes, because git
prints a line for a ref it left alone as readily as for one it changed:

| Flag on the extra line | Established | Boundary |
| --- | --- | --- |
| space, `+`, `-`, `*` | it was updated | `exceeded` → `PUSH_WROTE_UNEXPECTED_REFS` (74) |
| `!` `[remote failure]`, unrecognised | nothing | `unknown` → `PUSH_BOUNDARY_NOT_VERIFIED` (75) |
| `=`, `!` with a recognised refusal | it was *not* updated | `clean`; reported in the reasons, the run continues |

Exit 74 therefore means *a write beyond authority was established*, not merely
that another ref appeared in the output — the same standard `PUSH_FAILED`
holds itself to in the other direction.

**The boundary is a second fact, not a replacement for the first.** What the
push did to the authorised branch and what it did to everything else are
separate questions, and either can be known while the other is not. So the
branch is always read back *before* the boundary is judged, and both are
reported: `boundary_status` in the JSON, its own line in the text output, and
`pushed_sha` still populated when the read-back saw the commit. An earlier
version returned on an unresolved extra ref before reading the branch at all,
and answered "unknown" about a commit that was demonstrably sitting on it.

**This is not a force push**, despite the flag's name. The commit's parent is
already proven to be the reviewed head, so the update it asks for is an
ordinary fast-forward; the lease adds only the condition *update this ref iff
it is still exactly H*. The expected value is pinned explicitly rather than
left to the remote-tracking ref, which is what an unqualified
`--force-with-lease` consults — a local cache, and a lease against a cache is
a lease against whatever this clone last fetched.

The boundary test distinguishes the two precisely:

| | |
| --- | --- |
| `--force` | forbidden |
| `+<refspec>` | forbidden |
| unqualified `--force-with-lease` | forbidden |
| `--force-with-lease=refs/heads/<derived branch>:<40-hex>` | the only form, built in one module from values it has already validated |

### Push verification

`git push` exiting zero is not evidence that a ref moved. After the push the
remote is asked, with `git ls-remote`, what the branch now points at, and the
answer must be the exact commit this run created:

```text
created_fix_commit_sha == remote PR head SHA
```

That read-back also settles the ambiguous case in the *other* direction: a
push whose answer was lost still moved the ref, so a reported failure whose
read-back shows our commit is reported as a push that landed, not as a
failure.

A read-back that is *neither* our commit nor obviously unchanged is not
self-explanatory, and it is not treated as if it were. Two independent
questions are asked, because neither alone is enough.

**What did the remote say?** The push runs with `--porcelain`, which writes
one machine-readable line per ref on stdout whether it succeeded or failed:

```text
To <url>
!	<sha>:refs/heads/<branch>	[rejected] (non-fast-forward)
Done
```

The flag character is the first field. `!` marks a ref that was **rejected or
failed to push** — and that "or" is why the summary text is read rather than
the flag alone:

| Summary | Meaning |
| --- | --- |
| `[rejected]`, `[remote rejected]`, `[no match]` | the remote refused; nothing was written |
| `[remote failure]`, anything else after `!` | a server-side error whose outcome is **not** established |
| `=` `[up to date]` | the ref already held this commit; the push moved nothing |
| space, `*`, `+` | the remote applied our update |

Only a recognised refusal is evidence. Treating every `!` as a refusal would
turn a transient `[remote failure]` into a machine-readable claim that nothing
was written, which is the strongest claim this runner makes and the one it
must never make on a guess — so unrecognised summaries fail closed to
"unknown". Because that text is human-readable, git is run in the **C locale**
throughout, so a translated build cannot change the classification.

**An absent commit does not establish a no-write** either, because a commit
can land and then be erased from the branch's history. A local `pre-push` hook
refusal, a dropped connection and a timeout all produce *no* per-ref line at
all, and none of those is reported as though it were a refusal.

**A timeout after the push process starts is never a pre-write failure.** The
remote may already have applied the update, with the answer being the only
thing that went missing, so it becomes an attempted push with no answer rather
than a workspace error. The rule stated plainly:

```text
failure before the push process starts  => may be a verified no-write
failure after  the push process starts  => unknown until remote evidence proves otherwise
```

**Is our commit in the branch's history?**

```text
git rev-list --max-count=1 <our commit> ^<observed tip>
```

Empty means our commit is an ancestor of what the branch now holds, so the
push **did** land and the branch has moved on.

Together:

| Remote's answer | Commit in history | Outcome |
| --- | --- | --- |
| accepted | the ref *is* our commit | pushed **by this run**; CI wait begins |
| up to date, or no answer | the ref *is* our commit | the branch holds the fix, but this run is not credited with moving the ref; CI wait begins |
| rejected | no | `PUSH_FAILED` — verified no-write, on the remote's own word |
| any | yes | `CI_STALE_TARGET` — landed, branch moved on |
| accepted | no | `CI_STALE_TARGET` — landed, branch since rewritten |
| unknown or silent | no, or unanswerable | `PUSH_NOT_VERIFIED` — genuinely unknown |

The second row is a provenance rule rather than a safety one. `= [up to date]`
means the ref already held this exact commit and the push moved nothing, and a
missing answer means the runner cannot show its own push is what put it there.
The branch state is reported either way; only the attribution changes — and
the text output says *"the branch already held this exact fix; this run did
not move the ref"* rather than crediting an earlier run, because another actor
could equally have placed it.

**The same rule applies when the commit is merely an ancestor.** Finding it in
the branch's history proves it is there, not that this run put it there: a
concurrent runner can push the identical commit, advance past it, and leave
our own lease-guarded push refused — which looks exactly like this from here.
So `push_performed` is set from the remote's answer in that case too, never
from the ancestry alone.

The last row is the honest one and the reason the table exists: after a push
that produced no per-ref answer, an absent commit is compatible both with a
push that never landed and with one that landed and was erased. The runner
reports that it does not know, rather than putting a guess in a
machine-readable field.

### Authoritative CI, bound to the pushed commit

The wait re-uses PR #28's verification unchanged — including its definition of
authoritative evidence, so a `push`-event run on the same commit is still not
evidence, and a path-filtered workflow's absence is still explained or
ambiguous. What this stage adds is one requirement: **the pull request head
that verification resolved must be the exact commit that was pushed.**

* CI from the reviewed head is never accepted, even when it is green.
* A third commit pushed on top ends the run as `CI_STALE_TARGET`.
* Immediately after a push GitHub can still report the previous head; that is
  lag and is waited out, boundedly. If the head never becomes the pushed
  commit within `--ci-timeout`, the run ends `CI_AMBIGUOUS` rather than
  guessing.

The result vocabulary reuses the verification verdicts rather than inventing
a second set of words for the same facts — READY, FAILED, PENDING,
STALE_TARGET, AMBIGUOUS — with a `CI_` prefix marking which side of the push
they describe.

**Merge-context currency is back in contract here.** A fix turn deliberately
did not guarantee it, because a fix turn wrote nothing; this stage writes, so
it does. `PUSH_READY` requires that authoritative CI tested the pushed commit
merged onto the *current* base branch tip. Green CI against a merge that no
longer exists is `CI_STALE_TARGET`, and a fresh Independent Re-Review may not
start from it.

### Failure semantics

Every outcome answers "what is now different?" on its own, without being read
alongside anything else.

| Exit | Outcome | Repository state |
| --- | --- | --- |
| 0 | `PUSH_READY` | **Pushed.** CI green for the exact commit, against the current merge context. |
| 0 | `PUSH_PREPARED` | This run wrote nothing. `--dry-run` verified and applied the patch in a throwaway worktree. |
| 60 | `PUSH_INPUT_INVALID` | This run wrote nothing. |
| 61 | `PUSH_BRANCH_REFUSED` | This run wrote nothing. Fork head, closed pull request, default branch, an unusable branch name, or a remote naming another repository. |
| 62 | `PUSH_TARGET_STALE` | This run wrote nothing. The head moved, or the branch is somewhere unaccounted for. |
| 63 | `PATCH_IDENTITY_MISMATCH` | This run wrote nothing. |
| 64 | `COMMIT_REFUSED` | This run wrote nothing. |
| 65 | `PUSH_FAILED` | **This run wrote nothing**, verified: the remote's own `--porcelain` report *refused* the ref (a recognised rejection, not merely a failure). |
| 66 | `PUSH_NOT_VERIFIED` | **Unknown.** The remote gave no answer, or one that establishes nothing (`[remote failure]`, a timeout, a local hook). An absent commit proves nothing. Read the branch before doing anything else. |
| 67 | `CI_FAILED` | **Pushed.** CI for the exact commit failed. |
| 68 | `CI_PENDING` | **Pushed.** CI had not finished within `--ci-timeout`. |
| 69 | `CI_STALE_TARGET` | **Pushed.** The head moved off it, its merge context is stale, or a lost response hid a push that landed — under a later commit, or under a rewrite. |
| 70 | `CI_AMBIGUOUS` | **Pushed.** CI state undecidable. |
| 71 | `PUSH_WORKSPACE_INVALID` | This run wrote nothing. |
| 72 | `PUSH_API_ERROR` | This run wrote nothing. GitHub unreachable before the push. |
| 73 | `CI_API_ERROR` | **Pushed.** GitHub unreachable while waiting. |
| 74 | `PUSH_WROTE_UNEXPECTED_REFS` | **Written, beyond authority.** The remote reported *updating* a ref this run did not ask for. What the authorised branch holds is reported too. Inspect the remote. |
| 75 | `PUSH_BOUNDARY_NOT_VERIFIED` | **Boundary unknown.** The remote reported *trying* a ref this run did not ask for, with an answer that settles nothing. What the authorised branch holds is reported separately. Inspect the remote. |

Three rules the runner keeps on the failure paths:

* **Partial success is stated, never rounded.** A failure after a verified
  push reports the exact pushed SHA, that the branch was mutated, and why the
  observation did not complete. It never claims a rollback that did not happen.
* **Nothing is repaired automatically.** No second commit is created to fix a
  bad first one, no force push recovers a diverged branch, and no revert is
  written. Those are human decisions.
* **A local commit is not repository state.** A commit created in a worktree
  this run then removes is reported as created and *not* as written.

### Idempotency

There is no state file, no lock and no memory of previous runs — deliberately,
because a runner that remembered having pushed would be wrong exactly when it
mattered. A retry re-derives which situation it is in from git and GitHub:

| Observed | Concluded | Done |
| --- | --- | --- |
| branch tip == reviewed head | not committed, not pushed | apply, commit, push, wait |
| branch tip's parent == reviewed head **and** its diff hashes to the candidate digest | **this exact fix is already pushed** | nothing committed, nothing pushed; resume at the CI wait |
| branch tip is anything else | unaccounted for | `PUSH_TARGET_STALE`; a human looks |

The middle row is the whole idempotency boundary, and it is identity by
*content*: a commit is this fix if and only if its parent is the reviewed head
and its diff is the candidate patch. Commit SHAs are not comparable across
runs — the committer timestamp differs — so they are not what is compared.
`already_pushed: true` in the JSON, and "was already on the branch" in the
text, say which row was taken.

A local commit in a prepared worktree is never reused, because the worktree
does not survive the run. `--commit-cwd` is the exception an operator opts
into, and it is verified to be a clean checkout of the reviewed head first — a
directory holding a previous run's commit fails that check rather than being
built on.

### What a push turn enforces, and what it does not claim

| | |
| --- | --- |
| **Enforced by this runner** | The patch's bytes hash to the fix turn's digest. The applied tree and the created commit both re-hash to it. The commit's parent is the reviewed head and it is exactly one commit. The branch comes from GitHub's pull request object — read for the repository the handoff names, at both ends of the pull request — and cannot be a fork, a base or a default branch. Every URL for the chosen remote, fetch and push, names the target repository. The push is a fast-forward, conditional on the branch still being exactly the reviewed head. A no-write is claimed only on the remote's own recognised rejection; a failure after the push starts is never one. The pushed ref is read back from the remote. CI evidence belongs to the exact pushed commit, from the authoritative event, against the current merge context. |
| **Not enforced, and not claimed** | That the fix is *correct*. That it resolves the finding. That the pull request should be merged. That your git hooks do nothing else — they run, deliberately, and are trusted. That a digest survives configuration this module does not pin. That a push which landed and was then force-pushed away can be detected. Whether a `pre-push` hook, a signing configuration or a branch protection rule refuses the push — those are the remote's and the operator's decisions, reported rather than bypassed. |

## Re-reviewing the pushed fix: `review-loop re-review`

`PUSH_READY` means *a re-review could start here*. This is the command that
starts one.

```bash
review-loop push --fix-json fix.json --patch fix.patch --json > push.json

review-loop re-review --review-json review.json --push-json push.json \
  --reviewer-command "..."
```

What it does, in the order it does it:

```text
validated review + PUSH_READY push
→ revalidate the pushed fix against GitHub
→ fresh Independent Re-Review of that exact commit
→ original finding resolutions + fresh findings
→ re-verify the target
→ one '## Independent AI Re-Review' comment
```

### Two facts, never one

The whole design of this stage is a refusal to answer one question where
there are two:

```text
Did each original finding get resolved?          (history, about F1, F2, …)
Does the pull request now contain findings?      (a fresh review of now)
```

Neither implies the other, and collapsing them loses information in both
directions. A fresh Major finding in the fix does not make `F1` unresolved —
`F1` was resolved, and something else is now wrong. Every original finding
being resolved does not make the pull request clean — the fix may have
introduced something nobody has looked at.

So the contract carries two independent collections, the validator never
derives one from the other, the recorded comment renders them as two sections
with no combined total, and the JSON output has two keys and no status field.
The one place they legitimately meet is the reviewer's `Recommendation`, and
each coherence rule there names which collection it reads.

This is not hypothetical. PR #26's review found a real credential leak; PR
#27's found documentation that overclaimed. A fix that satisfies its finding
and introduces a provenance gap is the ordinary case, not the exotic one, and
a re-review that only ticked off the original findings would record that pull
request as clean.

### The `PUSH_READY` precondition, revalidated

The push document says the fix was pushed and green. That was true when the
push turn ended; a re-review costs minutes, and this is the most expensive
turn in the loop with the answer most likely to be acted on. So the claim is
re-established from GitHub before a reviewer starts:

| Checked | Failure |
| --- | --- |
| The pull request's head is exactly the pushed fix SHA | `TARGET_NOT_AT_FIX` |
| It still targets the base branch the fix was pushed against | `TARGET_NOT_AT_FIX` |
| Authoritative CI for that exact head verifies `READY` **now** | `TARGET_NOT_READY` |
| The merge CI tested is still the base branch tip | `TARGET_NOT_AT_FIX` |
| GitHub is reachable | `API_ERROR` |

In every one of those cases **no reviewer is started**. The head check comes
before the CI verdict deliberately: "the pull request is not at the fix any
more" and "its CI is not green" send an operator to look at different things,
and reporting the second when the first is true is a wrong instruction.

The last row is the one that is easy to miss. `READY` already requires that
the base tip and the merge base agree — except when the base tip could not be
read at all, which `READY` tolerates. The re-review asserts merge-context
currency itself rather than inheriting it, exactly as `review-loop push` does
before reporting `PUSH_READY`.

`TARGET_NOT_READY` reports the underlying verification verdict's own exit
code, so the `PENDING` / `FAILED` / `AMBIGUOUS` / `STALE_TARGET` vocabulary is
not duplicated.

### The inputs

Two documents, both machine-generated by earlier turns, both re-read through
the invariants that produced them:

* `--review-json` — a `review-loop review --json` document. Read by
  `review_loop.routing`, the *same* loader `review-loop fix` uses, so there is
  one implementation of "is this a validated review?" in the package. It must
  report `REVIEW_VALID` or `COMMENT_ALREADY_EXISTS`, round 1, at least one
  open finding, and `changes_requested`.
* `--push-json` — a `review-loop push --json` document. It must report
  `PUSH_READY`, not a dry run, `repository_mutated: true`, a `clean` write
  boundary, a full 40-character `pushed_sha` that is not the reviewed head, a
  `verified_target` at that SHA, and CI that was `READY`, bound to the pushed
  commit, and tested against what was then the base tip.

They must describe the same repository, the same pull request, and the same
reviewed head. When the push document reports a commit it created, its parent
must be exactly the reviewed head.

That last check is the tightest mechanical link this pipeline has between a
fix and the review it answers, and it is worth being explicit about its
limit: it establishes that the fix commit sits directly on the reviewed
commit, in this pull request, with CI verified against the current merge.
**It does not establish that the patch addresses those findings.** Nothing
mechanical can. That is the question the fresh re-review exists to answer,
and asserting it in the handoff would make the re-review ceremonial.

Both documents are operator-controlled input, exactly as the earlier handoffs
are. Anyone who can write them can choose which review and which push are
paired — but they cannot make a reviewer read a commit that is not the pull
request's head, because the runner re-derives that from GitHub and the
workspace resolves `refs/pull/N/head` from the remote. The files select; git
and GitHub decide.

### Round semantics

A **round is one Independent Review turn**, and this is the one place the
whole loop's numbering is stated:

```text
round 1   the initial Independent Review
          the fix turn routed from it        (still round 1)
          the push of that fix               (still round 1)
round 2   the fresh Independent Re-Review of the pushed fix
```

The fix and push turns do not start rounds of their own — they carry the
round of the review whose findings they act on, which is why a push handoff
must report `round: 1`. A later multi-round slice increments the same way, so
nothing here needs a second numbering scheme to grow into.

The round is part of the record identity, so a re-review adds evidence beside
the round-1 review rather than overwriting it.

### Finding identity

Original finding ids are preserved exactly. If the round-1 review raised `F1`,
`F2` and `F3`, the re-review reports resolutions against those three ids, in
those spellings. They are never renumbered.

Fresh findings are namespaced by the round that raised them: `R2.F1`,
`R2.F2`, and so on. The prefix is checked, and so is the collision:

* The prefix constrains **this round's reviewer**, which is what makes a
  fresh id recognisable on sight in the record.
* It says nothing about round 1, whose reviewer was never told to avoid the
  namespace and was free to name a finding `R2.F1`.

So every fresh id is also checked against the actual original ids, and that
second check is what makes the separation a guarantee rather than a naming
habit.

### Bounded Re-Review Response v1

The same shape as the Structured Verdict, with two kinds of block:

```text
BEGIN BOUNDED RE-REVIEW RESPONSE v1
Round: 2
Reviewed head SHA: <the exact 40-character pushed fix SHA>
Recommendation: <approved | changes_requested | escalate>
Escalation reason: <only when escalating with nothing else to escalate>
Finding ID: F1
Resolution: RESOLVED
Evidence: <what at this commit shows it>
Reason: <required for UNRESOLVED and ESCALATE>
Finding ID: F2
Resolution: UNRESOLVED
Evidence: <what shows it is still true>
Reason: <what the fix did not do>
Fresh finding ID: R2.F1
Severity: <Blocking | Major | Minor>
Location: <file path, with a line or symbol>
Problem: <what is wrong>
Evidence: <what shows it is wrong>
Required outcome: <what must be true for this to be resolved>
Scope boundary: <optional>
END BOUNDED RE-REVIEW RESPONSE v1
```

Parsing follows the same three rules as the verdict parser — only the
delimited block is read, a label counts only at column 0, and an unrecognised
label-shaped line at column 0 is an error rather than content. Two rules are
specific to this contract:

* **Two different openers.** `Finding ID` opens a resolution; `Fresh finding
  ID` opens a fresh finding. Different words, so a fresh finding cannot be
  reported as a resolution by accident, and `Evidence` — spelled the same in
  both — lands in the block its opener chose.
* **Resolutions come first.** Once a fresh finding has opened, a `Finding ID`
  line is an error. The ordering costs the reviewer nothing and makes the
  section a line belongs to decidable without lookahead.

`Resolution` admits exactly three words: `RESOLVED`, `UNRESOLVED`,
`ESCALATE`. There is deliberately **no `NEW_FINDING`** — a problem the fix
introduced is a fresh finding, and saying it here would overwrite the
historical fact about the original.

### Resolution validation

Mechanical, and failing closed:

* Every original finding id appears **exactly once**. None omitted, none
  duplicated, none unknown. A re-review that silently drops `F2` is not a
  re-review with a gap; it is a document that would let `F2` disappear.
* `Reviewed head SHA` is exactly the pushed fix SHA. Abbreviated, absent or
  merely close is `RE_REVIEW_SHA_MISMATCH`, not a weaker binding — including
  a response bound to the *original reviewed head*, which is the near miss
  this stage is most exposed to.
* `Round` is exactly 2.
* `Evidence` is required for every resolution, `RESOLVED` included. "Fixed"
  without the code that shows it is an assertion.
* `Reason` is required for `UNRESOLVED` and `ESCALATE`, and optional for
  `RESOLVED`. An unresolved finding has to say what the fix did not do; an
  escalation has to say what the human is being asked.

### Fresh finding validation

Fresh findings are validated by `verdict_validation.validate_finding` — the
Structured Verdict's own finding rules, called directly rather than
reimplemented. A fresh finding is admissible exactly when the same finding
would have been admissible in a round-1 verdict: the same closed severity
vocabulary, the same required fields, the same field limits, the same refusal
to let reviewer text contain the record marker's substrings, the same id
pattern. On top of that come the namespace prefix, the collision check
against the original ids, and uniqueness within the turn.

### Recommendation coherence

The one field that reads both collections, so each rule names its source:

| Recommendation | Requires |
| --- | --- |
| `approved` | every original `RESOLVED` **and** zero fresh findings |
| `changes_requested` | at least one non-`RESOLVED` original **or** at least one fresh finding |
| `escalate` | an `ESCALATE` resolution, a fresh `Blocking` finding, or an explicit `Escalation reason` |

A fresh `Blocking` finding always escalates — this project's standing
review-automation decision, unchanged. An `ESCALATE` resolution escalates
too: a question for a human is not a change to request.

### Post-review revalidation

The reviewer read one merge context. Before anything is recorded, the pull
request is verified again and compared with the target the reviewer was
given: same pull request, same head, same base branch, same merge base. If
any of that moved, the outcome is `TARGET_STALE` and **nothing is written** —
the re-review is real evidence about a state that is no longer current, and
recording it as though it described the pull request now would be the one
mislabelling this whole design exists to prevent.

### The recorded comment

```markdown
## Independent AI Re-Review

Round: 2
Reviewed head SHA: <pushed fix>
Fix for: round 1 review of <reviewed head>
CI integration base: master at <merge base>
CI verification: READY — .github/workflows/pytest.yml (run 42: success)
Recommendation: changes_requested

These are two independent facts. …

Original findings: 2
RESOLVED: F1, F2
UNRESOLVED: (none)
ESCALATE: (none)

Fresh Blocking: 0
Fresh Major: 1
Fresh Minor: 0
Fresh findings: 1

Original finding resolutions (round 1):

### RESOLVED — F1
…

Fresh findings:

### Major — R2.F1
…
```

Only validated fields are rendered. Whatever prose the reviewer wrote around
its block never reaches GitHub. The comment closes by saying what it is: not
an approval, and not a merge decision.

Note what the counts do **not** do. `Fresh Blocking` / `Fresh Major` /
`Fresh Minor` count fresh findings only. An unresolved original keeps the
severity round 1 gave it, and that severity belongs to the round-1 record;
counting it here would silently re-raise a finding this turn did not
independently make.

### Identity and idempotency

The same marker as a review record, with two fields carrying the difference:

```text
head  = the pushed fix SHA   (not the reviewed head)
round = 2
role  = independent-re-reviewer
```

Consequences, all tested:

* A re-review never overwrites, and is never mistaken for, the round-1 review
  of the commit it followed.
* A retry of the exact same re-review finds its own record and writes nothing
  — the check runs before the reviewer, to avoid paying for a review that
  cannot be posted, and again immediately before the write, which is the one
  that catches a retry whose earlier `POST` succeeded and whose response was
  lost.
* **The same pushed head against a different merge context is not a
  duplicate.** `base_sha` is part of the identity, so a record of the fix
  merged onto `B1` does not suppress a re-review of the same fix merged onto
  `B2`. That is a different integration state, verified by different CI, that
  nobody has re-reviewed — and it is exactly the bug the review turn's
  identity model was corrected for.
* A marker copied into someone else's comment does not suppress anything: a
  record is a matching marker **from the account this runner would post as**.
  That rule now lives in one place, `comment_format.find_record`, shared by
  both turns.

### Failure semantics

| Situation | Outcome | Exit | Comment written |
| --- | --- | --- | --- |
| A validated re-review | `RE_REVIEW_VALID` | 0 | one |
| Already recorded | `COMMENT_ALREADY_EXISTS` | 0 | none |
| Inputs are not a review + the `PUSH_READY` push of its fix | `RE_REVIEW_INPUT_INVALID` | 80 | none |
| The pull request moved off the fix, was retargeted, or its CI evidence is stale | `TARGET_NOT_AT_FIX` | 81 | none |
| CI for the fix is not `READY` now | `TARGET_NOT_READY` | the verification verdict's own | none |
| The reviewer's directory is not the fix | `REVIEWER_WORKSPACE_INVALID` | 82 | none |
| The reviewer failed or timed out | `REVIEWER_FAILED` | 83 | none |
| Unparseable, or a contract rule failed | `RE_REVIEW_MALFORMED` | 84 | none |
| The response names another commit | `RE_REVIEW_SHA_MISMATCH` | 85 | none |
| The pull request moved during the reviewer turn | `TARGET_STALE` | 86 | none |
| Valid, but the `POST` failed | `GITHUB_WRITE_FAILED` | 87 | none |
| GitHub unreachable | `API_ERROR` | 88 | none |

Two rows are worth reading twice.

**An unresolved original finding is not a failure.** `F1 → UNRESOLVED` with
no fresh findings is a *valid re-review*: exit 0, one comment, and the fact
that the fix did not solve the problem recorded as evidence. Nothing is
retried, no Coding Agent is invoked, and no second fix round starts.

**A fresh finding is not a failure either.** `F1 → RESOLVED` beside a fresh
Major is exit 0 and one comment carrying both facts. Whether that finding
gets fixed is a human's decision.

Exit code 0 means *a validated re-review exists for this exact pushed fix*.
It does not mean the findings were resolved and it does not mean anything may
merge.

### What a re-review turn does not do

It does not route a second fix, does not invoke a Coding Agent, does not push,
does not merge, and does not produce a Merge Decision Brief. It does not
increase the reviewer's authority in any way: the same subprocess contract,
the same read-only instruction, the same allowlisted environment, no
credential of its own, no push authority, no GitHub write. The runner
performs the single write, one issue comment, on the one path that ends in a
validated re-review.

And it does not decide. `Blocking = 0` and `Major = 0` and every original
`RESOLVED` are three pieces of evidence. Accepting them, deciding whether an
unresolved finding gets another attempt, deciding whether a fresh finding is
worth fixing, and merging are all a human's, and there is no code path here
that could take any of them.

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

The push-turn tests go further still, because this is the stage that writes.
Every git invariant is exercised against a real bare "remote" on disk: a real
`git apply`, a real commit whose parent and diff are read back from git, a
real `git push`, a real `git ls-remote` read-back, a real non-fast-forward
rejection when someone else moves the branch first, and a real second clone
used to prove the patch digest does not depend on which repository computed
it. After every refusal the test asserts the remote branch is still exactly
where it was. GitHub, by contrast, *is* faked — with a client whose head SHA
and CI answer a test moves between polls, from an injected `sleep`, so a
half-hour bounded wait costs no wall-clock time and the timeline under test is
written out explicitly.

The re-review tests reuse all of that rather than starting a second
framework: the same GitHub fakes, the same comment reader and writer, the
same reviewer stand-in, and the same real-git fixtures. The two inputs are
built as the documents the earlier commands actually emit, so a test that
changes one field changes exactly one fact. The workspace assertion is the
one that most needs real git — a re-reviewer that read the *reviewed* commit
rather than the fix would report every finding unresolved, correctly, about
the wrong tree — so a real fix commit is published as `refs/pull/N/head`, a
real detached worktree is prepared, and the reviewer reports the `git
rev-parse HEAD` and file contents it actually found.

Evidence separation has explicit regression coverage: `F1 → RESOLVED` beside
a fresh Major asserts both facts independently and asserts that the record
does not report `F1` as unresolved; `F1 → UNRESOLVED` with no fresh finding
asserts that nothing is invented.

## Known limitations

* **Scope is coarse, and can refuse legitimate findings.** A reviewer that
  describes a location without naming a path, or that names only paths outside
  the pull request's change set, gets `REVIEW_REQUIRES_HUMAN` rather than a
  guess. That is the intended direction, but it means a legitimate finding —
  "your change here breaks the caller over there" — does not route without an
  explicit `--allow-path`. A component root is also wider than most fixes
  need: within `tools/review-loop/` the scope check would not catch an
  unrelated edit to a neighbouring file in the same package.
* **The change-set boundary needs the remote, every run.** The base tip is
  fetched rather than read from a local `origin/<base>`, so a fix turn cannot
  run fully offline: no reachable remote means
  `CODING_AGENT_WORKSPACE_INVALID` rather than a fallback to a cached ref. That
  is deliberate — a cached base tip is how base-only changes leak into the
  boundary — but it does mean one small fetch per turn, and it means
  `--agent-cwd` now sees a `git fetch` into the repository you supplied.
* **The fix is checked for shape and place, never for correctness.** The
  runner establishes that the change is the one that was asked for, where it
  was allowed, and no more. Whether it actually satisfies the reviewer's
  `Required outcome` is not mechanised, and the agent's reported
  `Verification` is a claim that is deliberately not re-run.
* **A fix turn is not sandboxed.** See
  [What is structurally enforced](#what-is-structurally-enforced-and-what-is-only-asked-for).
  The worktree binds where the agent is pointed, not what it can reach.
* **A fix turn still ends at a patch.** `review-loop fix` commits nothing;
  without `--write-patch` the change is discarded with the worktree, and
  `review-loop push` needs that file. Whether the patch is used remains the
  human's decision, taken between the two commands.
* **Merge-context currency is out of contract *for a fix turn*.** It gates on
  head currency alone; the review's original merge/CI context is not
  re-established, and the base may have moved since. See
  [What a fix turn guarantees](#what-a-fix-turn-guarantees-and-what-it-does-not).
  A deliberate narrowing, not an oversight — but it means a candidate patch is
  never evidence that the pull request is currently green. `review-loop push`
  re-establishes it, because it writes.
* **The loop stops at one re-review.** A fresh Independent Re-Review of the
  pushed fix now exists, and it reports whether each original finding was
  resolved and what a fresh review of the current state found. What does not
  exist is anything that *acts* on that: no second Coding Agent round, no
  automatic routing of an unresolved or fresh finding, no multi-round loop,
  no Merge Decision Brief and no merge. `RE_REVIEW_VALID` means "here is
  evidence about this exact commit", not "this is done".
* **A run can end with the repository changed and the answer unknown.**
  `PUSH_NOT_VERIFIED` is a real outcome, not a defensive one: a push that
  exits zero and does not read back as the created commit leaves state this
  runner cannot determine. It reports that and stops, rather than pushing
  again to find out. Recovery is a human's.
* **Nothing is repaired automatically.** A bad commit is not amended, a
  diverged branch is not force-pushed back, and a failed CI run produces no
  follow-up commit. Every repair is a human decision, and by design there is
  no code path here that could take one.
* **Push authority is only as narrow as the pull request object.** The branch
  is derived from GitHub's `head.ref` for the target pull request, with fork,
  base and default-branch heads refused, both ends of the pull request
  required to be in the target repository, and every one of the remote's URLs
  required to name it. That is a strong bound, but it does rest on GitHub
  returning a truthful pull request object for a number the operator supplied
  in the handoff — and, for a hostless remote URL, on a local path that ends
  in `<owner>/<name>.git` actually being that repository.
* **Your git hooks run, and are trusted.** `--no-verify` is not passed to
  either `commit` or `push`, so a `pre-commit`, `prepare-commit-msg` or
  `pre-push` hook executes as normal and can do anything a program can. The
  structural write guarantee covers the argv this runner constructs, not
  arbitrary configured hook behaviour. A hook that edits files is still caught
  by the post-commit digest check.
* **A silent or unrecognised push failure leaves remote state genuinely
  unknown.** When the remote gives no per-ref answer — a local `pre-push` hook
  refusal, a dropped connection, a timeout — or gives one that establishes
  nothing, such as `[remote failure]`, an absent commit proves nothing,
  because a commit can land and then be erased. The run reports
  `PUSH_NOT_VERIFIED` rather than guessing, which means an operator has to
  look at the branch themselves. That is the intended direction, but it does
  mean an ordinary local hook refusal reports as "unknown" rather than as the
  no-write it almost certainly was.
* **The porcelain summary text is a parsing contract.** Refusals are matched
  against a fixed list of C-locale summaries, and git is run with `LC_ALL=C`
  so a translated build cannot change the classification. A future git that
  renames a summary would fail closed to "unknown" rather than misclassify —
  but it would also stop recognising a genuine refusal.
* **A `--commit-cwd` directory is written to and committed in.** It is
  verified to be a clean checkout of the reviewed head first, but the fix
  commit is created there and pushed from there. The prepared worktree is the
  default for exactly that reason.
* **A force-pushed base branch can move the boundary.** The change set is
  `merge-base(base, head)..head`. A base that merely advances leaves it
  unchanged, but one rewritten so the old divergence point is no longer an
  ancestor yields a different merge base, and therefore a different boundary.
  Nothing detects that; the run would simply be bounded differently.
* **Two rounds, and only in one shape.** The review turn accepts `Round: 1`
  and the re-review turn accepts `Round: 2`, and nothing accepts a third. A
  second fix routed from a re-review's findings, and the loop that would
  follow, are not implemented — the numbering and the record identity are
  designed to extend that way, but no code does it yet.
* **Whether a fix resolves a finding is a reviewer's judgement, not a
  mechanised one.** The runner establishes which commit was read, that every
  original finding was answered exactly once, and that each answer carries
  evidence. It does not check the answer. A re-reviewer that says `RESOLVED`
  without looking produces a well-formed record, exactly as a round-1
  reviewer that invents findings does.
* **The link between a fix and the findings it answers is structural, not
  semantic.** The pairing establishes that the fix commit's parent is the
  reviewed head, in this pull request, with CI verified. Nothing establishes
  that the patch inside it addresses those findings — which is why the
  re-review is a real review turn and not a checkbox.
* **A re-review of a fix pushed by someone else is indistinguishable.** The
  push document selects which commit is called "the fix"; git and GitHub
  confirm it is the pull request's head with the reviewed head as its parent,
  and nothing distinguishes a commit this pipeline pushed from a hand-written
  one in the same position. That is usually what you want, and it does mean
  `PUSH_READY` provenance is a claim of the document, not of the commit.
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
request state, one bounded local fix routed from it, that fix committed and
pushed to the pull request's own branch with authoritative CI observed for
the exact pushed commit, and one fresh Independent Re-Review of that exact
commit reporting original finding resolution and fresh findings as two
separate facts". Out of scope here, and left for later slices: a second
Coding Agent round, automatic routing of an unresolved or fresh finding, the
multi-round loop, the Merge Decision Brief, automatic merge, force-push
recovery, general-purpose branch write support, any server or daemon, and any
persistent state.

Stated as the pipeline:

```text
PR #34
Validated Finding
→ Candidate Patch

PR #35
Candidate Patch
→ Exact Fix Commit
→ Push
→ Authoritative CI

this slice
PUSH_READY
→ Fresh Independent Re-Review
→ Finding Resolution Evidence
+ Fresh Findings

not implemented
→ Automatic additional fix round
→ Multi-round loop
→ Merge Decision Brief
→ Merge
```

**The full Finding → Fix → Re-Review loop is still not automated.** Every
stage of it now exists, and nothing joins them: a re-review that reports an
unresolved original finding, or a fresh Blocking one, produces a record and
stops. What is automated is routing, bounded local fixing, getting a
validated fix onto the branch with its CI observed, and producing fresh
independent evidence about that fix — with a human still deciding whether the
finding was right, whether the fix is right, whether anything gets another
attempt, and whether anything merges.

That live trial has now happened, on PR #30, and is recorded in
[`docs/delegated-development/review-loop-live-experiment-1.md`](../../docs/delegated-development/review-loop-live-experiment-1.md).
It concluded that the flow does replace starting a review by hand, and named
the unbound reviewer working directory as the one thing to fix first — which
is what "Where the reviewer runs" above now does. That fix was then validated
live, against a pull request in a different repository, in
[`docs/delegated-development/review-loop-live-experiment-2.md`](../../docs/delegated-development/review-loop-live-experiment-2.md).

Structured Findings → Coding Agent routing + Bounded Fix Response is what
`review-loop fix` above now does.

Bounded fix → exact fix commit identity → push → wait for authoritative CI is
what `review-loop push` above now does, and it is the first stage that can
change this repository.

`PUSH_READY` → fresh Independent Re-Review → finding resolution evidence is
what `review-loop re-review` above now does. The next slices are an
additional fix round routed from a re-review's findings, the multi-round
loop, and the Merge Decision Brief. None of that is here, and the human gate
is why: this pipeline automates *routing, bounded local fixing, getting a
validated fix onto the branch, and producing independent evidence about it*.
It does not automate acceptance.
