# Review Loop Runner — Second Live Experiment

This document records a development-process experiment: the first
end-to-end run of `tools/review-loop` against a real open pull request in
**another repository**, and the first live test of the reviewer
workspace-binding fix that
[`review-loop-live-experiment-1.md`](review-loop-live-experiment-1.md)
asked for.

Experiment #1 established that a real reviewer's result could be bound to a
verified pull request state and recorded safely. It also found one
substantive gap: the runner proved which SHA a verdict *named*, but not
which tree the reviewer had *read*. A reviewer pointed at a stale directory
would echo the target SHA back out of its own prompt and pass every
downstream check. That gap was closed in
[PR #32](https://github.com/takolab/local-agent-concierge/pull/32), which
made the reviewer's working directory part of the review target.

This experiment asks whether that fix holds up operationally, against a
pull request the runner was not developed around.

It is a process record, not a design document for the runner. The runner's
design and contract are documented in
[`tools/review-loop/README.md`](../../tools/review-loop/README.md).

## Objective

Decide whether a local Coding Agent, acting only as the experiment
operator, can run the current `review-loop` end-to-end against a real pull
request in a different repository **without any human worktree or
`--reviewer-cwd` preparation**, while preserving exact-target provenance
and the existing safety boundaries.

Experiment #1 needed a hand-built detached worktree passed as
`--reviewer-cwd`. That step was necessary, undocumented, and easy to get
wrong. The question here is whether the runner now does it itself, and
whether it refuses when the workspace is wrong.

The question is **not** whether the reviewer's findings about the target
pull request are good, and no finding it produced was acted on.

## Setup

| | |
| --- | --- |
| Date | 2026-09-04 |
| Runner revision under test | `5c32f7cfe3ac97732add03bf9ce30abd89600bcc` |
| Runner test suite | 370/370 passing locally before the run |
| Target repository | `takolab/mapgram-backend` — a different repository from the runner's own |
| Target | [PR #3](https://github.com/takolab/mapgram-backend/pull/3) — `Add real GCS deletion job persistence adapter`, `codex/gcs-deletion-job-store-v1` → `main` |
| Target state | open and unmerged, before and after |
| Reviewed head SHA | `3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab` |
| CI integration base | `main` at `53f66585db59169035413ec28d2d50eb6cdebec5` |
| Authoritative CI evidence | `.github/workflows/ci.yml` run `33884123952` attempt 1, `event=pull_request`, `success` |
| Reviewer command category | A locally installed general-purpose coding-agent CLI, run non-interactively (prompt on stdin, verdict on stdout), restricted to read-only tools |
| Credentials | None created or changed. GitHub access was the existing `gh auth login` session; the reviewer used the CLI's own already-present login |

### Runner provenance, as it stood at execution time

The runner code that executed was `5c32f7c`, the tip of
`feat/review-loop-reviewer-workspace-binding`. The operator's working copy
was checked out at that commit and had not been fetched, so the experiment
was run and reported believing the branch was still unmerged.

It was not: PR #32 had merged that branch into `master` at 13:40:38Z,
roughly three hours before the first command in this experiment ran at
16:43Z. The stale local checkout changed nothing about what was executed —
`tools/review-loop/` is byte-identical between `5c32f7c` and the resulting
`master` at `bd9c3d196f64e3d011f7f6d68a1f82efa1f1a57d` — but it is recorded
here rather than quietly corrected, because "which revision did this
evidence come from" is the same class of question the runner exists to
answer precisely. See [Historical state versus current
state](#historical-state-versus-current-state) below.

### Why this target was chosen

Experiment #1 ran against this repository's own PR #30, so the runner was
reading the repository it was written in. That leaves the obvious question
unanswered: how much of its CI verification was really general, and how
much was shaped by one repository's conventions?

`takolab/mapgram-backend` PR #3 differs on every axis that the verification
contract touches. Its base branch is `main`, not `master`. Its workflow
file is `.github/workflows/ci.yml`, not `pytest.yml`. It has a different
workflow layout, and it is a TypeScript project rather than a Python one.
It was also open, unmerged, green, and small enough to review completely.

It is somebody else's real pull request, not a target manufactured for this
experiment. Its merge or closure is a human decision and was deliberately
left alone.

### Operator and reviewer separation

The local Coding Agent acted **only as the experiment operator**. It did
not review PR #3, and no analysis of its own entered the verdict. Its
responsibilities were to inspect the runner contract, verify the target,
configure and invoke the reviewer, observe behaviour, and audit side
effects.

The Independent AI Review was performed by a separate process with a fresh
context: the locally installed coding-agent CLI at
`/home/taiki/.claude/remote/ccd-cli/2.1.260`, invoked non-interactively
with the prompt on stdin and the verdict read from stdout, under the
runner's environment allowlist, authenticating from its own pre-existing
login through `HOME`.

No new API token, GitHub PAT, repository secret, paid API integration, GCP
credential or IAM permission was created for this experiment.

### Reviewer trust boundary

The reviewer was configured read-only by its own flags: permission prompts
disabled so that anything outside the allowlist is denied rather than
queued for a human, tools limited to file reads, search, and read-only
`git` subcommands, with editing, writing, and network tools explicitly
denied.

Two bounded probes confirmed that contract before the experiment began:

1. The stdin → stdout contract worked, and an attempted file write was
   denied with no file created.
2. Read-only `git` commands worked, and `git commit --allow-empty` was
   denied with no commit created.

As in Experiment #1, **that restriction is imposed by the reviewer command
on itself. It is not a boundary `review-loop` enforces**, and this
experiment provides no evidence that it is. The runner passes `PATH` and
`HOME` through its allowlist, so the reviewer ran as an ordinary child
process with the operator's permissions. This point has more force here
than in Experiment #1 and is taken up under [The GCP
boundary](#the-gcp-boundary) below.

## Step 1 — Baseline verification

`review-loop --repo takolab/mapgram-backend --pr 3` reported:

```text
PR:                   #3 (codex/gcs-deletion-job-store-v1 -> main)
Head SHA:             3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab  [3bb5a1c]
Head stable:          Yes
CI merge base:        53f66585db59169035413ec28d2d50eb6cdebec5
Merge base current:   Yes
Baseline workflows:   .github/workflows/ci.yml
Observed workflows:
  .github/workflows/ci.yml [REQUIRED] name='CI' run=33884123952 attempt=1
    event=pull_request status=completed conclusion=success
CI verdict:           READY
Reason:
  - .github/workflows/ci.yml (run 33884123952 attempt 1) succeeded
GitHub write performed: No
```

Exit code 0.

This is the first time the verification contract ran against a repository
it was not written for. It resolved a `main` base branch, classified
`ci.yml` as the required baseline workflow from that repository's own
workflow configuration read at the target commit, and found the single
authoritative `pull_request` run for the exact head. **No repository-specific
special-casing was needed, and none was added.** The generality PR #28
claimed for the verification contract — workflows identified by path from
the configuration at the head SHA, never by count or by name — held on a
repository that had never exercised it.

## Step 2 — Fail-closed negative control

The explicit `--reviewer-cwd` escape hatch was tested before the successful
path, because a fail-closed check is worth more when it has not yet been
given a target it would accept.

A clean detached worktree was created at the **base** commit
`53f66585db59169035413ec28d2d50eb6cdebec5` — deliberately not the review
target. It satisfied every cleanliness condition the runner checks: empty
`git status --porcelain --untracked-files=all`, empty
`git ls-files --others --ignored --exclude-standard`. Its only defect was
being the wrong commit.

Run live, with no `--dry-run`:

```text
Outcome:              REVIEWER_WORKSPACE_INVALID
Reason:
  - the reviewer working directory '.../negctl' is at
    53f66585db59169035413ec28d2d50eb6cdebec5, not the review target
    3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab; a review of it would describe
    another commit
Reviewer invoked:     No
GitHub write performed: No
```

Exit code 35, in roughly four seconds. The reviewer was never started and
nothing was written. The temporary negative-control worktree was removed
afterwards.

This is the exact failure Experiment #1 said was undetectable. Under the
old contract that directory would have been accepted, the reviewer would
have reviewed the base commit, and the verdict would have named the head
SHA anyway — because the SHA comes from the prompt, not from what was read.

## Step 3 — Dry run with the runner-managed workspace

`review-loop review --repo takolab/mapgram-backend --pr 3 --repo-root ...
--reviewer-command ... --dry-run`, 16:43:45Z → 16:49:52Z (6m07s).

No `--reviewer-cwd` was passed, and no worktree was prepared by hand.

```text
Reviewer workspace:   a detached worktree at the target, from .../mapgram-backend
Reviewer invoked:     Yes
Verdict:              round=1 recommendation=changes_requested open=2
                      (Blocking 0 / Major 1 / Minor 1)
Reviewed head SHA:    3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
Reason:
  - dry run: the review is valid and would be recorded
GitHub write performed: No
```

Exit code 0. The Structured Verdict parsed and passed every validation rule
on the first attempt. No validation was weakened and no retry was needed.
The pull request still had zero comments afterwards.

The dry run printed the full comment it would have recorded, including the
identity marker, which is what made the rendering checkable before anything
was written.

## Step 4 — Live review

The same command without `--dry-run`, 16:50:27Z → 16:56:02Z (5m35s).

```text
Reviewer workspace:   a detached worktree at the target, from .../mapgram-backend
Reviewer invoked:     Yes
Verdict:              round=1 recommendation=changes_requested open=1
                      (Blocking 0 / Major 1 / Minor 0)
Reviewed head SHA:    3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
GitHub write performed: Yes (comment 5543813716)
```

Exit code 0. Exactly one GitHub write.

Result on GitHub: one `## Independent AI Review` comment
([5543813716](https://github.com/takolab/mapgram-backend/pull/3#issuecomment-5543813716)),
authored by the account the runner resolved as its own, with
`created_at == updated_at` — never edited — and carrying exactly one hidden
identity marker:

```text
<!-- local-agent-concierge:independent-review:v1 repo=takolab/mapgram-backend
     pr=3 head=3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab
     base=53f66585db59169035413ec28d2d50eb6cdebec5 round=1
     role=independent-reviewer -->
```

The body displayed the exact reviewed head SHA, the CI integration base,
the authoritative CI run, the recommendation and the severity counts,
rendered from validated fields only. `pulls/3/reviews` remained `0`: an
issue comment was created, not a review object, as designed.

Per this experiment's boundary, the finding was **not** fixed, not routed
to a Coding Agent, and not otherwise acted on. It remains on PR #3 as
ordinary Independent Review evidence for a human.

## Step 5 — Idempotency retry

The identical command was run again immediately against the unchanged
target, 16:56:09Z → 16:56:12Z.

```text
Reviewer invoked:     No
Outcome:              COMMENT_ALREADY_EXISTS
Reason:
  - comment 5543813716 already records round 1 of this review for
    3bb5a1ce61301a63d99b368c3c3c6db6b8e907ab
GitHub write performed: No
```

Exit code 0 in three seconds instead of another five and a half minutes.
Zero additional comments, zero additional writes. Duplicate suppression
happens before the reviewer is started, so the retry cost nothing.
Afterwards the pull request still had exactly one comment, from one author,
carrying one marker.

## Workspace binding — the central evidence

This is what Experiment #2 existed to test, so the evidence is set out in
full. Three independent lines support the conclusion that the reviewer read
the target commit, and they fail differently: one is the runner's own
claim, one is the content of the review, and one was written by the
reviewer process without either the runner or the operator asking for it.

**Evidence 1 — the runner's own report.** The runner printed
`Reviewer workspace: a detached worktree at the target, from
.../mapgram-backend`, and then `Reviewed head SHA: 3bb5a1ce... (matches
target)`. This is the weakest of the three on its own: it is the runner
asserting that it did what it says it does.

**Evidence 2 — the reviewer cited content that exists only at the target.**
The verdict located its findings in
`src/account-deletion/gcs-persistence.ts` at specific line ranges,
including `114-122`, `273-304` and `373-390`. The operator verified against
git that this file:

* does not exist at the base commit `53f6658` — `git cat-file -e` reports
  `fatal: Not a valid object name`
* exists at the target head `3bb5a1c`
* is 423 lines long there

A reviewer that had merely echoed the SHA out of its prompt could not have
produced line-range citations inside a 423-line file that exists at no
other commit in the repository. Whatever tree it read, that tree contained
the target's own content.

**Evidence 3 — reviewer-side working-directory residue.** The reviewer
process independently created session-state directories named after its own
working directory, of the form:

```text
/tmp/claude-1000/-tmp-review-loop-pr3-<rand>-3bb5a1ce6130
```

That path is the mangled form of the runner-prepared worktree
`/tmp/review-loop-pr3-<rand>-3bb5a1ce6130`, and it encodes the target SHA.
This is the strongest line, because **the runner did not write it**: it is
the reviewer process's own record of the directory it was launched in,
produced as a side effect rather than as a claim. Exactly two such
directories existed — one for the dry-run invocation, one for the live
invocation, and none for the idempotency retry, which matches the three
runs' reported `Reviewer invoked` values precisely.

The operator supplied `--repo`, `--pr`, `--repo-root` and
`--reviewer-command`, and nothing else. **No human worktree or
`--reviewer-cwd` preparation was required**, which is the step Experiment
#1 had to perform by hand.

### Runner workspace cleanup

The runner's own temporary worktrees were removed cleanly. After the
experiment:

* no `/tmp/review-loop-pr3-*` worktree remained
* `git worktree list` showed only the ordinary clone
* `.git/worktrees` was absent
* the clone's `HEAD` never moved off its original branch and commit

Separately, and outside the runner's cleanup contract, the reviewer's own
`/tmp/claude-1000/...` session state remained. These are two different
things and are not conflated here: **the runner's cleanup succeeded**; the
residue that survived was created by the reviewer subprocess, whose local
writes the runner has never claimed to manage. It is recorded as an
operational observation under [Findings about the
runner](#findings-about-the-runner).

## Reviewer variability between the dry run and the live run

The dry run and the live run were two independent reviewer invocations —
the dry run's result is not cached — and they did **not** return identical
verdicts.

| | Dry run | Live run |
| --- | --- | --- |
| Recommendation | `changes_requested` | `changes_requested` |
| Blocking | 0 | 0 |
| Major | 1 | 1 |
| Minor | 1 | 0 |

Both converged on related 404-handling concerns in the same adapter, but
framed them differently: the dry run reported them as two findings of
different severity, while the live run reported a single Major finding that
merged the 404 gap and extended it to a second call site.

This is worth preserving as an operational fact rather than a defect: **a
dry run is not a deterministic preview of the verdict a later live
invocation will produce.** It is a valid check of the pipeline, the
workspace binding, the validation rules and the comment rendering. It is
not a preview of the content that will be posted. Experiment #1 observed
the same convergence-without-identity across its two invocations; this run
shows the divergence can extend to the finding count and severity split,
not only the wording.

Nothing in the runner promises otherwise, and neither run was invalid.

## Side effects

The experiment changed nothing other than the single review comment.
Verified afterwards:

* PR #3 remained open and unmerged
* the head SHA did not move — still `3bb5a1ce...`, still 1 commit and 7
  changed files, base still `main` at `53f66585...`
* no PR code was changed and no finding was fixed
* no branch was pushed — `mapgram-backend`'s remote branches were still
  exactly `main` and `codex/gcs-deletion-job-store-v1` at their original
  SHAs
* no tracked file in either repository was modified; all experiment state
  lived in a scratch directory
* no GCS request, no GCP resource access, no bucket access, no IAM change,
  no credential created or changed, no deployment, no production data
  accessed
* runner-created worktrees were cleaned up, and the negative-control
  worktree was removed
* local working copies were not moved

PR #3 explicitly has no live GCS verification in its scope, and that
boundary was preserved.

### The GCP boundary

One point needs stating precisely, because it is the kind of claim that is
easy to overstate.

`gcloud` and `gsutil` **were** on `PATH` in this environment, and the
runner passes `PATH` and `HOME` to the reviewer by design. So the absence
of GCP access here was **not structurally enforced by `review-loop`**. It
was a property of the reviewer command the operator configured — whose
`Bash` permissions admitted only read-only `git` subcommands, with
everything else denied — together with what was observed after the run:
nothing under either `gcloud` configuration tree had been modified during
the experiment window, the most recent entry predating it by weeks.

The same caveat applies to the reviewer's read-only behaviour generally. It
came from the reviewer's own configured tool restrictions, not from an
operating-system or container sandbox provided by `review-loop`. The
runner's README says exactly this; what this experiment adds is that on
this machine the risk is concrete rather than theoretical, because
credentialed cloud tooling really is reachable on the reviewer's `PATH`.

**Do not read the observed absence of GCP access as a guarantee the runner
provides.** It is not one.

## Findings about the runner

**The Experiment #1 provenance gap is closed, and closed in the strong
direction.** The runner now prepares the workspace itself, so the ergonomic
path is also the correct one, and the operator cannot forget a step they
are no longer asked to perform. The escape hatch is verified rather than
trusted, and it failed closed on the first attempt against a workspace
whose only defect was being the wrong commit.

**Runner-enforced guarantees observed here.** The runner fetched
`refs/pull/3/head`, refused to proceed unless it resolved to exactly the
verified target SHA, created a detached worktree, verified that worktree
rather than assuming `git worktree add` had done the right thing, ran the
reviewer there, and removed it. Duplicate suppression fired before reviewer
invocation. Post-review revalidation confirmed an unchanged head and an
unchanged integration base before the write. A `--repo` / `--repo-root`
mismatch also fails closed — the fetched ref would resolve to a different
SHA and raise "the remote is not the repository under review" — though that
path was established by reading `reviewer_workspace.py`, not by a live run,
and is recorded here as such.

**Friction and follow-ups.** Four, none of which blocked the experiment and
none of which weakens a safety property:

1. **A misleading workspace label on the failure path.**
   `ExistingWorkspace.describe()` emits wording equivalent to
   `(verified against the target)`, and it is rendered before the
   verification actually runs. During the negative control that label
   therefore appeared a few lines above `Outcome:
   REVIEWER_WORKSPACE_INVALID`. The safety behaviour was correct; the
   output asserts a past-tense guarantee that did not hold, in exactly the
   text an operator reads while diagnosing. This is diagnostic wording, not
   validation logic.
2. **Reviewer-side temporary residue.** The runner removed its own
   worktrees correctly, but the reviewer left roughly 104 KB of session
   state under `/tmp/claude-1000/...`, keyed by the path of the now-deleted
   worktree. This is outside the runner's current cleanup contract, whose
   local-write claim is scoped to the package itself, so it is a boundary
   observation rather than a cleanup failure. It does mean PR number and
   target SHA persist in a local temporary path after the run finishes.
3. **The cross-repository local clone requirement is not obvious.**
   Reviewing another repository needs both `--repo` and a local clone
   supplied through `--repo-root`. The runner fails closed when the two
   would not agree, which is the right behaviour, but the README's
   description of `--repo-root` ("default: the current directory") does not
   make it clear that cross-repository use requires a clone at all.
   Experiment #1 never met this, because target and runner were the same
   repository. Documentation friction.
4. **Interpreter setup friction persists.** The machine's default `python3`
   is 3.10 against the package's `requires-python = ">=3.12"`. A Python
   3.12 environment created with `uv` worked. This is Experiment #1's first
   friction item, unchanged, and it is setup friction rather than a safety
   problem.

**Unexpected behaviour.** None. No malformed verdict, no SHA mismatch, no
stale target, no timeout, and no API error across the five runs.

## Limitations of this experiment

- **One pull request, one reviewer, one round**, as in Experiment #1. PR #3
  is a real code change rather than a documentation diff, which is a
  broader surface than Experiment #1 exercised, but it is still a single
  data point in a single repository.
- **The three evidence lines are strong but not a proof of exclusivity.**
  They establish that the reviewer read a tree containing the target's
  content, and that it was launched in the runner-prepared worktree. They
  do not establish that it read *only* that tree. As the README says, the
  guarantee is about the directory the reviewer is given, not about what it
  chooses to open; a reviewer is free to read elsewhere on the filesystem
  and nothing here would notice.
- **The negative control tested one kind of wrong workspace.** A clean
  checkout at the wrong commit was refused. The dirty-working-tree and
  git-ignored-file refusals remain covered by tests only; no live
  `--reviewer-cwd` run exercised them.
- **Reviewer read-only behaviour and GCP non-access were observed, not
  enforced.** See [The GCP boundary](#the-gcp-boundary).
- **No failure path other than `REVIEWER_WORKSPACE_INVALID` was exercised
  live here.** Every other run took the success path.
  `REVIEW_MALFORMED`, `REVIEW_SHA_MISMATCH`, `TARGET_STALE` and
  `GITHUB_WRITE_FAILED` gained no new live evidence from this experiment.
- **"Independent" is bounded in the same way as before.** The reviewer ran
  in a fresh process with a fresh context and no knowledge of the
  implementing session. It is not vendor independence, and the reviewer
  here shares a model family and account with the operator.
- **Review authorship is still not distinguishable on GitHub.** The
  provenance check verifies the comment came from the account the runner
  posts as; it cannot separate that account's automated records from its
  hand-written ones.

## Historical state versus current state

Recorded so that later readers can tell what was true when, rather than
reconstructing it from a merge graph.

| | When Experiment #2 ran (2026-09-04, ~16:43–16:56Z) | Now |
| --- | --- | --- |
| Runner code executed | `5c32f7c`, tip of `feat/review-loop-reviewer-workspace-binding` | same code, reached via `master` |
| Operator's working copy | detached on that branch, unfetched | — |
| Believed merge state | branch believed unmerged | PR #32 merged at 13:40:38Z, before the run |
| `master` | `bd9c3d1` (already, unbeknownst to the operator) | `bd9c3d1` |
| Feature branch on the remote | already deleted | deleted |

The two facts that matter:

* **`tools/review-loop/` is byte-identical between `5c32f7c` and
  `bd9c3d1`.** `git diff 5c32f7c origin/master -- tools/review-loop/`
  is empty, so the code this experiment exercised is the code on `master`
  today. The evidence transfers without qualification.
* **The experiment was reported at the time as testing an unmerged
  branch.** That was wrong, and it was wrong because the operator read a
  stale local checkout instead of re-verifying against the remote. It
  changed no result. It is left in the record rather than smoothed over,
  because it is a small live instance of the failure mode this whole tool
  is built to refuse: trusting a local view of state that has since moved.

Nothing else in this document has been restated in terms of the current
repository. The runs, SHAs, timings and outputs above are as observed.

## Conclusion

**A — Validated.**

The workspace-binding fix works operationally, in a repository the runner
was not developed around, and the exact-target Independent AI Review
foundation is now validated by live evidence rather than by tests alone.

The evidence supports each of the following:

* a local Coding Agent operated the full experiment end-to-end **without
  any human worktree or `--reviewer-cwd` setup**
* cross-repository CI verification succeeded, on a `main` base branch and a
  `ci.yml` workflow, with no repository-specific special-casing
* the runner itself bound the reviewer's workspace to the exact target SHA
* the Independent Reviewer demonstrably read target-only content
* a wrong `--reviewer-cwd` failed closed at exit 35, before reviewer
  invocation and before any write
* Structured Verdict validation succeeded on the first attempt
* post-review target revalidation succeeded
* exactly one GitHub review comment was recorded, with correct identity
* the same-target retry was idempotent and skipped reviewer invocation
* no unrelated or production side effects occurred

The provenance gap Experiment #1 identified is therefore **operationally
closed, not merely test-covered**. The four friction items are diagnostic
wording, a residue boundary, and two documentation gaps; none invalidates
the experiment and none blocks the next slice.

## Next recommendation

Proceed to the next bounded automation slice:

```text
Structured Findings → Coding Agent routing → Bounded Fix Response
```

Experiment #1 deferred that slice behind one fix, on the grounds that
"automatic fix routing consumes findings, and findings from a reviewer that
may not have read the reviewed commit are the wrong thing to route." That
condition is now satisfied: the findings this runner produces come from a
reviewer whose working directory is verified to be the commit under review.

This document records only that the evidence supports progression. The next
slice is not designed here.

The friction items above are follow-ups, not preconditions. Item 1 is a
one-line wording fix worth making whenever the runner is next touched;
items 2–4 are README and cleanup-contract notes.
