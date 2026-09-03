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
review-loop --pr 27 --dry-run
```

```text
PR:                   #27 (feat/orchestrator-dispatch-correlation-logging -> master)
Head SHA:             3b514700c1c2c257a39a7037f1a21ca5b9064106  [3b51470]
Head stable:          Yes
Baseline workflows:   .github/workflows/pytest.yml
Observed workflows:
  .github/workflows/orchestrator.yml [CONDITIONAL] name='Orchestrator tests' run=33730139664 attempt=1 event=pull_request status=completed conclusion=success
  .github/workflows/pytest.yml [REQUIRED] name='Python tests' run=33730139593 attempt=1 event=pull_request status=completed conclusion=success
CI verdict:           READY
Reason:
  - .github/workflows/orchestrator.yml (run 33730139664 attempt 1) succeeded
  - .github/workflows/pytest.yml (run 33730139593 attempt 1) succeeded
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
| `READY` | 0 | The head resolved, did not move during verification, the baseline workflow ran for that exact commit, and every observed workflow completed successfully. |
| `PENDING` | 10 | A relevant run for the exact head is `queued`, `requested`, `waiting`, `in_progress` or `pending`. |
| `FAILED` | 11 | A relevant run completed with `failure`, `timed_out`, `startup_failure`, `cancelled`, `action_required` or `stale`. |
| `AMBIGUOUS` | 12 | CI state could not be determined safely — see below. |
| `STALE_TARGET` | 13 | The head SHA changed between selecting the target and verifying it. |
| `API_ERROR` | 20 | GitHub could not be queried. |
| — | 2 | CLI usage error. |

Only exit code `0` means a review may be started, so `review-loop --pr N` is
directly usable as a shell condition. Every other code is a distinct non-zero
value, so an automation can also branch on *why* it is not ready.

The runner fails closed: anything it cannot explain becomes `AMBIGUOUS` rather
than collapsing into `READY`. `AMBIGUOUS` covers a missing baseline run, a
workflow whose `pull_request` trigger could not be interpreted, a run for a
workflow absent from the configuration at that commit, one workflow path
reported under several workflow ids, an unrecognised status or conclusion, and
runs returned for a commit other than the one queried.

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
* **`CONDITIONAL`** — path-filtered. Its absence may be legitimate, so absence
  is not held against the commit.
* **`NOT_EXPECTED`** — no `pull_request` trigger for this base branch.
* **`UNKNOWN`** — the trigger block could not be interpreted → `AMBIGUOUS`.

No workflow count is ever assumed. Path filters make the number of runs vary
between pull requests: at the time of writing, PRs #26 and #27 each produced
two runs out of three configured workflows.

The runner does **not** re-implement GitHub's path-filter matching. It only
decides whether a filter exists, which is enough to know whether an absence
needs explaining.

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

The head is read twice: once to choose the target, and once after the runs and
workflow configuration have been collected. If it moved in between, the
evidence describes a commit that is no longer the review target, and the
verdict is `STALE_TARGET`. A green result for a superseded commit is never
reported as `READY`.

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
