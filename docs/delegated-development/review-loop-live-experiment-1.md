# Review Loop Runner — First Live Experiment

This document records a development-process experiment: the first
end-to-end run of `tools/review-loop`'s one-turn Independent AI Review
runner against a real open pull request and a real AI reviewer. Until this
run, every part of that flow had been exercised only against fakes.

It is a process record, not a design document for the runner. The runner's
design and contract are documented in
[`tools/review-loop/README.md`](../../tools/review-loop/README.md).

## Objective

Decide whether `review-loop review --pr N` can actually replace the manual
review initiation this repository has been using — the workflow described
in [`level3-experiment-2.md`](level3-experiment-2.md), where the human
opens a separate AI context, prepares the review input by hand, reads the
result back, and pastes it into the pull request as a comment.

The question is **not** whether the AI reviewer's findings are good. It is
whether a real reviewer result can be bound to an exact pull request state
and recorded safely, with less human routing than the manual flow.

No new review-automation capability was implemented for this experiment.
The contract merged in
[PR #29](https://github.com/takolab/local-agent-concierge/pull/29) was used
unchanged.

## Setup

| | |
| --- | --- |
| Date | 2026-09-04 |
| Repository state | `master` at `6488a06ab134c335548ae5548f5494b9a4a79cfa` |
| Runner | `tools/review-loop`, 337 tests passing locally before the run |
| Target | [PR #30](https://github.com/takolab/local-agent-concierge/pull/30) — `docs/readme-repository-structure` → `master` |
| Reviewed head SHA | `ae9869c986c73ac373bbc5ec307ea31356632ba5` |
| CI integration base | `master` at `6488a06ab134c335548ae5548f5494b9a4a79cfa` |
| Authoritative CI evidence | `.github/workflows/pytest.yml` run `33868931566` attempt 1, `success` |
| Reviewer command category | A locally installed general-purpose coding-agent CLI, run non-interactively (prompt on stdin, result on stdout), restricted to read-only tools |
| Credentials | None created or changed. GitHub access was the existing `gh auth login` session; the reviewer used the CLI's own already-present login |

### Why a new pull request was created

No suitable open pull request existed — every prior one (#17–#29) was
already merged, and a merged pull request cannot report `READY` by design.
A minimal target was therefore created: a documentation-accuracy fix to the
root `README.md`, whose Repository Structure tree still described the tree
as it stood before `packages/`, `services/`, `infra/` and `tools/` existed.

It is a real fix rather than a fabricated diff, it changes no product,
production or deployment behaviour, it touches no secret or external
integration, and it is small enough to review completely. Its merge or
closure is a human decision and was deliberately left open.

### Reviewer trust boundary

The reviewer was configured with a read-only tool allowlist (file reads,
search, and read-only `git`/`gh` subcommands) and with permission prompts
disabled, so anything outside that allowlist is denied rather than queued
for a human. A probe run confirmed a file write was refused.

That restriction is imposed by the reviewer command on itself. **It is not
a boundary `review-loop` enforces**, and this experiment provides no
evidence that it is. As `tools/review-loop/README.md` states, the reviewer
runs as an ordinary child process with the invoking user's permissions and
can reach `~/.config/gh` and `~/.ssh` through `HOME`. The observation
available here is weaker and worth stating exactly: after the run, the
reviewer's working tree was clean, the pull request head was unchanged, and
the only comment on the pull request was the one the runner wrote.

## Step 1 — Baseline verification

`review-loop --pr 30`, run while CI was still building, reported:

```text
CI verdict:           PENDING
Reason:
  - .github/workflows/pytest.yml (run 33868931566 attempt 1) is in_progress
GitHub write performed: No
```

Exit code 10, no reviewer started. After CI completed (11:38:52Z →
11:42:01Z, about 3 minutes), the same command reported:

```text
Head SHA:             ae9869c986c73ac373bbc5ec307ea31356632ba5  [ae9869c]
Head stable:          Yes
CI merge base:        6488a06ab134c335548ae5548f5494b9a4a79cfa
Merge base current:   Yes
Baseline workflows:   .github/workflows/pytest.yml
CI verdict:           READY
Reason:
  - .github/workflows/pytest.yml (run 33868931566 attempt 1) succeeded
  - .github/workflows/agent-contracts.yml did not run: this diff misses its path filter
  - .github/workflows/orchestrator.yml did not run: this diff misses its path filter
  - .github/workflows/review-loop.yml did not run: this diff misses its path filter
```

Exit code 0. The three absent workflows were each explained against this
pull request's own changed files rather than assumed away — the path-filter
reasoning PR #28 built, exercised for the first time on a live docs-only
diff.

## Step 2 — Dry run with the real reviewer

`review-loop review --pr 30 --reviewer-command ... --dry-run`, 11:42:29Z →
11:45:59Z (3m30s).

```text
Reviewer invoked:     Yes
Verdict:              round=1 recommendation=changes_requested open=1 (Blocking 0 / Major 0 / Minor 1)
Reviewed head SHA:    ae9869c986c73ac373bbc5ec307ea31356632ba5 (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
GitHub write performed: No
```

The reviewer returned a Structured Verdict that passed the parser and every
validation rule on the first attempt: `Round: 1`, the exact 40-character
head SHA, a recommendation consistent with its one finding, and all
required per-finding fields present and non-empty. No validation was
weakened, and no retry was needed.

The dry run printed the full comment it would have recorded, which is what
made it possible to check the rendering before anything was written.

## Step 3 — Live review

The same command without `--dry-run`, 11:46:15Z → 11:49:41Z (3m26s).

```text
Reviewer invoked:     Yes
Verdict:              round=1 recommendation=changes_requested open=1 (Blocking 0 / Major 0 / Minor 1)
Reviewed head SHA:    ae9869c986c73ac373bbc5ec307ea31356632ba5 (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
GitHub write performed: Yes (comment 5540015916)
```

Result on GitHub: exactly one `## Independent AI Review` comment
([5540015916](https://github.com/takolab/local-agent-concierge/pull/30#issuecomment-5540015916)),
authored by the same account the runner resolved as its own, carrying the
hidden identity marker

```text
<!-- local-agent-concierge:independent-review:v1 repo=takolab/local-agent-concierge
     pr=30 head=ae9869c9... base=6488a06a... round=1 role=independent-reviewer -->
```

and displaying the exact reviewed head SHA, the CI integration base, the
authoritative CI run, the recommendation and the severity counts. The body
was rendered from validated fields only; the reviewer's raw output never
reached GitHub.

### What the reviewer found

One Minor finding, and it was correct. The pull request had added a
paragraph asserting that "`services/` holds the containerized runtime
services". The reviewer checked that claim against `docker-compose.yml` and
established that four containerized runtime services are built from this
repository — `hermes-agent` (`apps/`), `slack-gateway` (`apps/`),
`google-calendar-mcp` (`mcp/`) and `orchestrator` (`services/`) — so only
one of the four is under `services/`. It cited the compose file's build
contexts by line, corroborated with `.github/workflows/pytest.yml` and two
existing design documents, and bounded the fix to that paragraph.

Two properties matter more than the finding itself:

* It **treated the pull request description as claims, not evidence.** The
  description asserted the tree and the documentation index were accurate;
  the reviewer verified both independently, confirmed them, and reported
  only the one added claim the repository does not support.
* The dry run and the live run were **two independent reviewer
  invocations** — the dry run's result is not cached — and both converged
  on the same finding, with the live run supplying more precise
  line-level evidence.

Per this experiment's boundary, the finding was **not** fixed here. No
Coding Agent was invoked, no fix was applied, no re-review was run. It
remains on PR #30 as ordinary Independent Review evidence for a human.

## Step 4 — Idempotency retry

The identical command was run again against the unchanged target,
11:50:10Z → 11:50:15Z.

```text
Reviewer invoked:     No
Outcome:              COMMENT_ALREADY_EXISTS
Reason:
  - comment 5540015916 already records round 1 of this review for ae9869c986c73ac373bbc5ec307ea31356632ba5
GitHub write performed: No
```

Exit code 0 in 5 seconds instead of 3m26s. Duplicate suppression works in a
live environment, and it happens *before* the reviewer is started, so a
retry costs nothing. Afterwards the pull request still had exactly one
comment.

## Comparison with the manual workflow

The manual flow recorded in `level3-experiment-2.md` is: human checks the
PR and CI → opens a separate AI context → prepares the review input by hand
→ reads the result → pastes it into the PR as a comment.

**Human steps removed.** Opening a second context, assembling the review
input, deciding which commit the review is about, copying the result back,
and formatting it as a comment. All of that is now one command.

**Human steps still required.** Choosing the target pull request, deciding
when to run the review, waiting for CI, and every decision after the review
— triaging findings, fixing, and merging. The runner initiates and records;
it decides nothing.

**Context switching.** Reduced. The manual flow required leaving the
terminal for a second tool and back again; this run never left the shell.

**Evidence quality.** Not degraded relative to the manual baseline. The
finding is specific, evidence-bearing and correctly severity-labelled,
which matches what the manual ChatGPT reviews produced on PRs #19 and #20.
One structural improvement over the manual flow: the record states which
exact commit and which CI integration base the review describes, which a
hand-pasted comment never did. One structural regression: the manual flow
produced prose the human had already read, whereas here the human reads the
finding for the first time in the comment.

**Trust and inspectability.** Better than the manual flow in a way that
does not depend on trusting the reviewer. A manual comment asserted "an
independent review said this"; this record binds the review to a
40-character SHA and a named CI run, and refuses to post at all if either
moved. What it still cannot do is prove *who* wrote it: the record is
posted under the same account as everything else in this repository, so the
role remains a convention, exactly as the README says.

**Friction observed.** Four things, none of which blocked the experiment:

1. **Interpreter version.** The README's `pip install -e "tools/review-loop[test]"`
   fails on this machine, whose default `python3` is 3.10 against the
   package's `requires-python = ">=3.12"`. A 3.12 virtual environment was
   needed. The failure message is clear, but the README does not mention it.
2. **Finding a reviewer command.** No reviewer was on `PATH`. The one used
   had to be located on disk. The runner is correct not to bundle a vendor
   integration, but "which command do I pass to `--reviewer-command`" is
   currently unanswered by any documentation.
3. **CI wait dominates.** `pytest.yml` has no path filter, so a one-file
   documentation change still ran the full Docker build. CI took about as
   long as the review itself. This is the repository's CI design, not the
   runner's behaviour — the runner correctly refused to start until it was
   green.
4. **Dry run doubles the cost.** `--dry-run` invokes the reviewer for real
   and does not cache the result, so dry-run-then-live means two full
   reviews, roughly seven minutes here. This is the documented design and
   it is what made the rendering checkable before writing, but it is a real
   cost once the flow is trusted.

No friction was observed in: reviewer authentication, the environment
allowlist, Structured Verdict formatting, timeouts, comment rendering, or
target staleness.

## Findings about the runner

**The runner binds the review to an exact SHA, but not the reviewer's view
of the code.** This is the one substantive gap the live run exposed. The
runner captures the exact head SHA and puts it in the prompt, and it
rejects any verdict naming a different commit — but the reviewer's actual
filesystem is `--reviewer-cwd`, which defaults to the current directory.
That directory is normally the operator's checkout, which is usually on
another branch, or on the target branch with uncommitted changes. Nothing
checks that it corresponds to the reviewed SHA.

For this experiment a detached `git worktree` at
`ae9869c986c73ac373bbc5ec307ea31356632ba5` was created and passed as
`--reviewer-cwd`, so the reviewer read exactly the commit under review.
That step was necessary and is documented nowhere. A reviewer pointed at a
stale directory would still produce a verdict that passes every validation
rule the runner has, because the SHA it echoes back comes from the prompt,
not from what it read. The runner's own README already names this class of
risk — "a reviewer that invents findings or claims to have read a commit it
did not produces a well-formed record" — and this is the concrete, easily
triggered instance of it.

**Everything else behaved as documented.** `PENDING` fail-closed with no
reviewer started, path-filter explanations on a live diff, first-attempt
verdict validation, exact SHA binding, unchanged-target revalidation,
single-comment write, and pre-reviewer duplicate suppression all worked on
the first live attempt, with no change to the PR #29 contract.

**Unexpected behaviour.** None. No malformed verdict, no SHA mismatch, no
stale target, no timeout, no API error, and no unexplained output across
the four runs.

## Limitations of this experiment

- **One pull request, one reviewer, one round.** A 45-line documentation
  diff is a small and unusually verifiable review surface. Nothing here
  shows how the flow behaves on a large or subtle code change.
- **The failure paths were not exercised live.** `REVIEW_MALFORMED`,
  `REVIEW_SHA_MISMATCH`, `TARGET_STALE` and `GITHUB_WRITE_FAILED` are
  covered by tests but were not observed against a real reviewer. In
  particular, no real reviewer has yet been observed *failing* to produce a
  valid verdict, so the claim "a real reviewer returns a Structured Verdict
  reliably" rests on two invocations.
- **"Independent" is bounded.** The reviewer ran in a fresh process with a
  fresh context and no knowledge of the implementing session, which is the
  independence the prompt asks for. It is not vendor independence: the
  reviewer and the agent that wrote the pull request are the same model
  family under the same account. The manual baseline used a different
  vendor, so this run is *less* independent in that respect.
- **Review authorship is still not distinguishable on GitHub**, exactly as
  in the earlier experiments. The record's provenance check verifies the
  comment came from the account the runner posts as; it cannot separate
  that account's automated records from its hand-written ones.
- **Reviewer read-only behaviour was observed, not enforced.** See the
  trust boundary note above.

## Next recommendation

**B — refine first, then proceed.**

The flow works. It replaced manual review initiation on a real pull request
on the first attempt, produced a correct and well-evidenced finding, bound
it to a verified state, recorded exactly one comment, and refused to
duplicate it — with no change to the PR #29 contract and no new credential.
That is the success criterion this experiment set, and it was met.

One bounded fix should land before the next slice, because it undermines
the guarantee the whole design is built on: **the runner should ensure the
reviewer actually reads the commit it is reviewing.** The narrow version is
enough — verify that `--reviewer-cwd` is a clean checkout of the target SHA
and fail closed if it is not, or prepare a detached worktree at the target
SHA and hand that to the reviewer. Everything else in this record is
documentation-sized: note the Python 3.12 requirement, and say what a
reviewer command looks like.

Recommended follow-ups, in order:

1. Bind the reviewer's working directory to the reviewed SHA (fail closed
   on mismatch).
2. Document the interpreter requirement and one concrete
   `--reviewer-command` example.
3. Then proceed to the next slice, **Structured Findings → Coding Agent
   routing + Bounded Fix Response**.

Deferring (1) past the next slice would be the wrong order: automatic fix
routing consumes findings, and findings from a reviewer that may not have
read the reviewed commit are the wrong thing to route.
