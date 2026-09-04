# Review loop runner

A local command that runs one Independent AI Review turn against a pull
request, and records the result only when it can prove which exact pull
request state that review describes.

It has two commands.

**`review-loop --pr N`** answers two questions, read-only:

1. **Which exact commit would be reviewed?** — the pull request's current
   40-character head SHA, not its merge commit and not its base.
2. **Is that exact commit's CI in a state where a review may start?**

**`review-loop review --pr N`** uses that answer to run the review itself:
capture the verified target, invoke an independent read-only reviewer, validate
the Structured Verdict it returns, re-verify that the target has not moved, and
record the result as one `## Independent AI Review` comment.

Everything else the review loop will eventually need — routing findings to a
Coding Agent, applying fixes, re-review, merge decisions — is not here. This
is one review turn, bound to one verified state.

## Verification: `review-loop --pr N`

```bash
pip install -e "tools/review-loop[test]"
review-loop --pr 28 --dry-run
```

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
4. **Run the reviewer** against that exact SHA.
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
review-loop review --pr 29 --reviewer-command "claude -p" --reviewer-env ANTHROPIC_API_KEY
```

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

## Tests

```bash
pip install -e "tools/review-loop[test]"
python -m pytest tools/review-loop/tests
```

No test performs network access, invokes a real reviewer, or requires
credentials; the GitHub API is replaced by fakes built from real recorded
response shapes, and the reviewer-process tests run `sys.executable` with an
inline script. CI needs no agent credentials.

## Known limitations

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
* **The reviewer is trusted, in two ways.** It is trusted to have actually
  reviewed — this runner validates the *shape* and *binding* of a verdict, not
  its truth, so a reviewer that invents findings or claims to have read a
  commit it did not produces a well-formed record. And it is trusted not to
  write: it runs as an ordinary child process with the invoking user's
  permissions and can reach `~/.config/gh` and `~/.ssh` through `HOME`. The
  structural write guarantee covers this package, not the command you point it
  at. A sandboxed, credential-less reviewer worktree is a later change.
* **No persistent state.** Everything is reconstructed from GitHub and the
  current invocation on each run; there is no database and no daemon.
* **Historical reviews are discarded.** If the target moves during the review,
  the result is dropped rather than recorded as evidence about the older
  commit.

## Scope boundary

This slice ends at "one validated review, recorded against one verified pull
request state". Out of scope here, and left for later slices: routing
Structured Findings to a Coding Agent, the Bounded Fix Response, applying
fixes, waiting for CI after a fix, Independent Re-Review, multi-round loops,
finding resolution tracking, the Merge Decision Brief, automatic merge, any
server or daemon, and any persistent state.

The next slice is **Structured Findings → Coding Agent routing + Bounded Fix
Response**. Before that, this one-turn flow should be used on a real pull
request to see whether it actually replaces starting a review by hand.
