# Review loop runner

A local, read-only command that answers two questions about a pull request
before an Independent AI Review is started against it:

1. **Which exact commit would be reviewed?** — the pull request's current
   40-character head SHA, not its merge commit and not its base.
2. **Is that exact commit's CI in a state where a review may start?**

It is deliberately small. It does not start a reviewer, does not generate or
parse findings, does not invoke a Coding Agent, and does not write anything to
GitHub. It is the correctness boundary those later pieces will sit on: if the
runner says `READY`, the review that follows is attached to a commit whose CI
state is actually known.

## Usage

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

`--dry-run` is currently a no-op, because every mode is already read-only. It
exists so the read-only verification path keeps a stable name if the runner
later grows a mode that does write.

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

## GitHub authentication and the read-only guarantee

The runner shells out to the already-authenticated `gh` CLI (`gh auth login`).
It introduces no new credential: no PAT, no GitHub App, no secret, and nothing
stored in the repository.

Every request goes through a single `gh api` call site that hard-codes
`--method GET`. No caller supplies an HTTP method, and no write endpoint is
referenced anywhere in the client. The runner cannot post a comment, submit a
review, change a label, push, dispatch or re-run a workflow, or merge. Tests
assert this at the source level.

## Tests

```bash
pip install -e "tools/review-loop[test]"
python -m pytest tools/review-loop/tests
```

No test performs network access or requires credentials; the GitHub API is
replaced by fakes built from real recorded response shapes.

## Scope boundary

This slice ends at "which exact commit, and may a review start against it".
The next slice — invoking an Independent Reviewer, producing Structured
Findings, routing them to a Coding Agent, and re-reviewing — consumes this
verdict but is not part of it. Out of scope here: reviewer invocation, finding
generation or parsing, PR comments or reviews, Coding Agent invocation,
automatic fixes, waiting for CI after a fix, multi-round loops, merge
decisions, any server or daemon, and any persistent state.
