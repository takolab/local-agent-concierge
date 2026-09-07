# Review Loop Runner — Third Live Experiment

This records the third live experiment with `tools/review-loop` against real
open pull requests, and the first one in which a recorded finding was acted
on by the pull request's own author. Stages 0 to 2 were exercised; it is not
an end-to-end run of the loop, which now has six stages.

It follows
[`review-loop-live-experiment-1.md`](review-loop-live-experiment-1.md) and
[`review-loop-live-experiment-2.md`](review-loop-live-experiment-2.md), and
it is the first run made after the Review Loop v1 consolidation audit
([PR #38](https://github.com/takolab/local-agent-concierge/pull/38)).

Two pull requests were used, for different reasons, and the difference
between them is most of what this experiment established. The runner's own
contracts are in
[`tools/review-loop/README.md`](../../tools/review-loop/README.md).

**Stages exercised: 0, 1 and 2.** `push`, `re-review` and `merge-brief`
still have no live evidence, and this run explains why reaching them is
harder than it looked.

## Objective

Decide whether the review loop, as consolidated by PR #38, produces useful
independent evidence on a pull request the operator did not write — and find
out what actually happens next when someone acts on that evidence.

Experiments #1 and #2 both reviewed a target chosen to be reviewable. Neither
followed what happened to the finding afterwards.

## Setup

| | |
| --- | --- |
| Date | 2026-09-07 |
| Runner | `tools/review-loop` at `8c5673aaa06f9f6c59ff32f75fef16f5b0b8edd1`, 1335 tests passing locally before the run |
| Interpreter | Python 3.12 in a throwaway virtualenv; the system `python3` is 3.10 and cannot install the package |
| Reviewer command category | A locally installed general-purpose coding-agent CLI, run non-interactively, restricted to read-only tools |
| Credentials | None created or changed. GitHub access was the existing `gh auth login` session; the reviewer used the CLI's own already-present login |
| GitHub writes | Two: one `## Independent AI Review` comment per target. No other write of any kind |

### Runner provenance

The runner executed was the head of
[PR #38](https://github.com/takolab/local-agent-concierge/pull/38), which has
since merged as `82a2cec`. `tools/review-loop/` is **byte-identical** between
`8c5673a` and `82a2cec`:

```bash
git diff 8c5673a 82a2cec -- tools/review-loop/   # empty
```

So every observation below describes the code now on `master`, exactly as
experiment #2 established this check.

### Two targets, chosen for different reasons

**`takolab/local-agent-concierge#38`** — the consolidation audit PR, written
by the same operator and agent that ran the loop. Chosen because it was the
only open pull request in this repository at the time. It is a poor
independent-review target for an obvious reason, and the experiment records
what that produced rather than pretending otherwise.

**`takolab/mapgram-backend#9`** — "Add public account deletion API boundary",
9 files, +2797/−15, written by a different agent in a different repository.
A public API boundary handling account deletion and provider
reauthentication: substantial, security-relevant, and not the operator's own
work. This is the target the experiment rests on.

### Reviewer trust boundary

The reviewer was configured with a read-only tool allowlist — file reads,
search, and read-only `git` subcommands — and with permission prompts
disabled, so anything outside the allowlist is denied rather than queued for
a human.

Two probe runs confirmed the shape before any target was touched: a file read
succeeded, a file write was refused, and `git log` / `git merge-base`
succeeded. The refused write left no file behind.

That restriction is imposed by the reviewer command on itself. **It is not a
boundary `review-loop` enforces**, and this experiment provides no evidence
that it is — the same statement experiments #1 and #2 make.

## Part 1 — `takolab/local-agent-concierge#38`

### Step 1 — Baseline verification

`review-loop --pr 38 --dry-run`:

```text
Head SHA:             8c5673aaa06f9f6c59ff32f75fef16f5b0b8edd1  [8c5673a]
Head stable:          Yes
CI merge base:        7057396b5c1c5904a8eb72a9d2bc3caad084dbc0
Merge base current:   Yes
CI verdict:           READY
Reason:
  - .github/workflows/pytest.yml (run 34100320472 attempt 1) succeeded
  - .github/workflows/review-loop.yml (run 34100320409 attempt 1) succeeded
  - .github/workflows/agent-contracts.yml did not run: this diff misses its path filter
  - .github/workflows/orchestrator.yml did not run: this diff misses its path filter
```

Exit code 0. The CI integration base was `7057396`, the merge commit of
PR #37 — so the loop verified itself against the exact state the audit had
just described.

### Step 2 — Dry run

09:19:55Z → 09:22:48Z (2m53s).

```text
Verdict:              round=1 recommendation=approved open=0 (Blocking 0 / Major 0 / Minor 0)
Reviewed head SHA:    8c5673aaa06f9f6c59ff32f75fef16f5b0b8edd1 (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
```

### Step 3 — Live review

09:23:38Z → 09:25:49Z (2m11s). `approved`, zero findings again.

Result on GitHub: one `## Independent AI Review` comment
([5568484292](https://github.com/takolab/local-agent-concierge/pull/38#issuecomment-5568484292)),
authored by the account the runner resolved as its own, with
`created_at == updated_at` — never edited — and carrying exactly one hidden
identity marker.

### Step 4 — Idempotency retry

Re-running with `--reviewer-command /bin/false`:

```text
Reviewer invoked:     No
Outcome:              COMMENT_ALREADY_EXISTS
Reason:
  - comment 5568484292 already records round 1 of this review for 8c5673aa...
```

Exit code 0 in **4 seconds**, and nothing was written.

Experiments #1 and #2 both established this behaviour live already, and
concluded the same thing: suppression happens before the reviewer starts, so
a retry costs nothing. What this run adds is only the method. `/bin/false` as
the reviewer turns "the reviewer was never started" from a line in the report
into a property of the run: the command succeeds *because* nothing tried to
execute it. Read as a reconfirmation on the consolidated v1 runner, with a
stronger control than the earlier two used.

### Step 5 — Routing an approved review

`review-loop fix --review-json review.json --agent-command /bin/false`:

```text
Agent invoked:        No
Outcome:              NO_ACTIONABLE_FINDINGS
Reason:
  - the review recommends 'approved' and reports no open finding
Patch:                (none)
GitHub write performed: No
Commit or push performed: No
```

Exit code 0, immediately. Again `/bin/false` proves no agent ran.

### What this half established

The loop behaves correctly on a clean pull request, and **stops there**. An
`approved` review is a dead end at stage 2 by design: `push`, `re-review` and
`merge-brief` all require at least one routable finding, and there was none.

The lesson is about target selection, not about the runner: **a self-authored
documentation-and-tests pull request cannot exercise the loop past `fix`.**
It should not have been expected to.

One claim made during this run and later withdrawn is worth recording,
because the mistake is easy to repeat. The audit had separately found a real
stale line in `reviewer_prompt.py` ("Re-review is not supported yet"), and
the approved review was initially read as a reviewer *recall failure*. It was
not: `reviewer_prompt.py` is **not in PR #38's change set**, and the reviewer
prompt instructs the reviewer to review *this pull request's own change set*.
The reviewer was correct. **A repo-wide audit and a pull request review answer
different questions, and a clean review is not evidence that the repository is
clean.**

## Part 2 — `takolab/mapgram-backend#9`

### Step 1 — Baseline verification

`review-loop --repo takolab/mapgram-backend --pr 9 --dry-run`:

```text
PR:                   #9 (codex/public-account-deletion-api -> main)
Head SHA:             f8fb48e26c3b1fe009e128475d651d075c3e70a3  [f8fb48e]
Head stable:          Yes
CI merge base:        e3a97ac4d09fa06053ccea5e8c61aa23b9e60e92
Merge base current:   Yes
CI verdict:           READY
Reason:
  - .github/workflows/ci.yml (run 34140008641 attempt 1) succeeded
```

Exit code 0.

### Step 2 — Cross-repository setup

Reviewing another repository needs `--repo` **and** a local clone supplied
through `--repo-root`. No clone existed, so one was made into a scratch
directory outside the operator's project tree.

Experiment #2 already recorded this requirement as a finding about the
runner's discoverability. It recurred here exactly as described, on a machine
that had run the loop before — which is mild evidence that the friction is in
the interface rather than in the operator's memory.

### Step 3 — Dry run

15:53:57Z → 16:00:25Z (6m28s).

```text
Verdict:              round=1 recommendation=changes_requested open=1 (Blocking 0 / Major 0 / Minor 1)
Outcome:              REVIEW_VALID
```

One **Minor** finding: the mutable `stage` variable used to attribute
unexpected failures in logged events is advanced to `"parse_request"` inside
the body-read callback and never updated again before the later validation
steps, so an unexpected exception raised during provider verification is
logged with the wrong stage.

That finding was independently checked against the code and is real. **It was
never recorded**, for the reason the next step makes clear.

### Step 4 — Live review

16:01:36Z → 16:06:51Z (5m15s).

```text
Verdict:              round=1 recommendation=changes_requested open=1 (Blocking 0 / Major 1 / Minor 0)
Reviewed head SHA:    f8fb48e26c3b1fe009e128475d651d075c3e70a3 (matches target)
Revalidation:         READY, target unchanged
Outcome:              REVIEW_VALID
GitHub write performed: Yes (comment 5573205076)
```

Exit code 0. Exactly one GitHub write:
[5573205076](https://github.com/takolab/mapgram-backend/pull/9#issuecomment-5573205076),
`created_at == updated_at`, one identity marker, and the only comment on the
pull request.

### What the reviewer found

A **Major** finding in `src/account-deletion/public-api.ts:292-293`
(`#validateGoogle`):

```ts
const identityLastSignIn = providerIdentity(user, "google")?.lastSignInAt;
const lastSignInAt = identityLastSignIn ?? user.lastSignInAt;
```

Google "reauthentication" falls back to the Supabase user's account-wide
`last_sign_in_at` when the Google identity's own timestamp is absent. A caller
presenting `{"reauthentication":{"provider":"google"}}` can therefore pass
purely because the account signed in recently through an unrelated method —
asymmetric with the Apple path, which cryptographically verifies a fresh
identity token. The finding also observed that the test fixture
`googleUser()` sets the identity-level and user-level timestamps together, so
the divergent branch is never exercised.

### Verifying the finding before routing it

The runner validates a verdict's **shape and binding**, never its truth — the
README says so explicitly. An operator check before acting on a recorded
finding is therefore part of the workflow, and here it mattered.

**Verified as accurate:** the fallback exists as quoted; the Apple/Google
asymmetry is real; and the test fixture does tie both timestamps to one
parameter, so the divergent case genuinely had no coverage.

**Inaccurate:** the finding's `Problem` says the code "contradicts ... the
design doc", quoting it as

> the linked Google identity's `last_sign_in_at` ... must be no more than
> five minutes old

The document actually reads:

> the linked Google identity's `last_sign_in_at`, **or the authenticated
> user's fallback value**, must be no more than five minutes old

The ellipsis elides exactly the clause that documents the fallback. The same
paragraph also says the server "preserves the current check" — that is,
deliberate continuity with production behaviour. **The design doc did not
contradict the code; it described it.**

This is not a runner defect. It is the documented contract working as
intended, and it shows what that contract costs: a recorded finding can be
substantively useful and still carry evidence that would mislead a reader who
took it at face value. The risk was concrete — an agent reading the comment
literally could have deleted a deliberate production behaviour in an
authentication path.

The finding's own `Required outcome` contained the correct escape hatch:
"or, if the fallback is intentionally preserved from existing production
behavior, add a test ... and update the documentation".

### Step 5 — The authoring agent's fix

The pull request's authoring agent was shown the comment and pushed
`dec4cc3f6337305a50f38f1eed9e37f893de76a2`, "Document Google reauthentication
fallback", at 16:17:40Z — 11 minutes after the review was recorded.

It took the `Required outcome`'s second branch:

| | |
| --- | --- |
| Code | unchanged except a two-line comment. The fallback was kept |
| Tests | +2, and a new `googleUserWithSignInTimes(identityLastSignInAt, userLastSignInAt)` fixture that decouples the two timestamps — the exact coupling the finding named. One test covers the fallback case, one asserts a fresh account-level timestamp does not override a stale identity-level one |
| README | "provider-specific reauthentication succeeds" → "validation succeeds", plus an explicit statement that the fallback "establishes recent activity on the same Supabase user, not independent proof that Google was the provider used" |
| Design doc | precedence made explicit, and the fallback's limitation and intent stated |

CI passed on the new head (`ci.yml` run 34142598130).

**This is the first time a finding recorded by this loop changed a pull
request.** The outcome was good: the untested branch gained coverage and the
README's overclaim was corrected. The misquote did not propagate into a wrong
code change — though nothing in the pipeline prevented that, and the operator
check is the only reason it was caught in advance.

## Reviewer variability: total divergence, not merely different framing

The dry run and the live run were two independent reviewer invocations of the
**same commit**, and they did not agree:

| | Dry run | Live run |
| --- | --- | --- |
| Recommendation | `changes_requested` | `changes_requested` |
| Blocking / Major / Minor | 0 / 0 / 1 | 0 / 1 / 0 |
| Subject | `stage` attribution in error logging | Google reauthentication fallback |
| File region | `:369-392`, `:577-585` | `:292-293` |

Experiment #2 observed divergence in count and severity while both runs
"converged on related 404-handling concerns in the same adapter". This run is
stronger: the two invocations found **entirely different problems in
different parts of the file**, with no overlap at all. Both findings were
independently verified as real.

Two consequences follow, and the second is the operational one:

1. A single review is a sample, not a census. Neither run was wrong; each
   found something the other missed.
2. **A dry run is not a preview, and its findings are discarded.** Nothing
   carries a dry-run verdict forward — the live run starts a fresh reviewer.
   The Minor `stage` finding here was verified as real and then lost, because
   the live invocation looked elsewhere. An operator who wants a verified
   dry-run finding must carry it by hand.

## An externally pushed fix is not re-reviewable through the supported workflow

The authoring agent's fix moved the head, and this is where the experiment
found something the consolidation audit could not.

`re-review` takes a `review-loop push --json` handoff. The supported way to
get one is to run `review-loop fix` and then `review-loop push`, and that path
is closed once another actor moves the head: `fix` refuses the stale review
target, so no genuine handoff is produced and `re-review` never becomes
reachable through the CLI.

Observed, not reasoned about — `review-loop fix` with the recorded review:

```text
Outcome:              CODING_AGENT_WORKSPACE_INVALID
Reason:
  - origin/refs/pull/9/head resolves to dec4cc3f6337305a50f38f1eed9e37f893de76a2,
    but the review target is f8fb48e26c3b1fe009e128475d651d075c3e70a3; the pull
    request moved, or the remote is not the repository under review
Agent invoked:        No
```

Exit 42, no agent started, nothing written.

| Stage | Once someone else pushes the fix |
| --- | --- |
| the recorded review | evidence about a commit the pull request left behind |
| `fix` | `CODING_AGENT_WORKSPACE_INVALID` |
| `re-review` | not reachable — the supported path produces no handoff |
| `merge-brief` | not reachable — it needs the re-review document |
| `review` | available: another **round 1** of the new head, recorded as a *second* comment, since the head is part of the record identity |

So round 2 in v1 is reachable **only for a fix the supported workflow itself
pushed**. The re-review stage is built to answer "did *the fix we pushed*
resolve these findings?", not "is this finding resolved now?".

### What this does *not* establish

It is worth stating the boundary of the claim, because provenance is one of
this runner's core concerns and it would be easy to read more into the
observation than it carries.

This experiment shows that the **supported CLI path** cannot continue once
another actor moves the head. It does **not** show that feeding an externally
pushed fix into `re-review` is technically impossible. `rereview_input.py`
treats both handoffs as operator-controlled input, and its checks —
`PUSH_READY`, `repository_mutated`, the exact pushed and parent SHAs, the
merge base, the patch digest, the review digest, the finding ids — are strong
*consistency* checks, not signatures. The README says as much under
*Provenance is carried, not proved*: an edited pair that agrees with itself is
accepted, and nothing distinguishes a commit this pipeline pushed from a
hand-written one in the same position.

A sufficiently privileged operator could therefore construct a mutually
consistent pair by hand. That is outside the supported workflow and proves
nothing about who performed the mutation, which is precisely why the limit is
stated as a property of the workflow rather than of provenance.

The README's "Two rounds, and only in one shape" limitation is adjacent but
says something different — it is about round numbering and a second fix
round. This limit is about *who pushed the fix*, and it was not stated
anywhere. It is now, added in
[PR #39](https://github.com/takolab/local-agent-concierge/pull/39).

**The consolidation audit could not have found this by reading.** It needed a
real external fix to a real recorded finding.

## Side effects

Checked after every run, on both targets:

* the reviewer's detached worktree was created under `TMPDIR` with the target
  SHA in its path, and was **removed on every path** — none remained;
* the operator's own checkout stayed clean and on its original commit;
* `git worktree list` showed only the ordinary clone afterwards;
* exactly one comment existed on each pull request, both authored by the
  account the runner resolved as its own, both with
  `created_at == updated_at`;
* no branch, tag, label, review, merge or workflow dispatch was created
  anywhere;
* the `mapgram-backend` clone was made in a scratch directory and never left
  `main`.

Total GitHub writes across the whole experiment: **two comments.**

## Findings about the runner

1. **The `approved` dead end is a target-selection trap, not a bug.** Nothing
   in the interface warns that an `approved` review ends the pipeline at
   stage 2. An operator setting out to exercise `push` onward will discover
   it only after paying for a review.
2. **The cross-repository clone requirement recurred.** Experiment #2 named
   it; it caught the same operator again. `--repo-root`'s help text still
   reads "default: the current directory", which does not suggest that
   cross-repository use requires a clone at all.
3. **`/bin/false` is a cheap and strong negative control.** Passing it as
   `--reviewer-command` or `--agent-command` turns "the reviewer was not
   started" from a claim in the output into a property of the run. Both the
   idempotency retry and the `NO_ACTIONABLE_FINDINGS` path were verified this
   way.
4. **The operator check between record and route is load-bearing.** The
   runner's refusal to validate truth is correct and documented, and it means
   a human step sits between a recorded finding and any action on it. This
   experiment is the first evidence that the step catches something.
5. **The supported workflow's external-fix limit was undocumented.** Real,
   and now stated in the README — as a limit on the CLI path rather than on
   what an operator could construct. See above.

## Limitations of this experiment

* **Stages 3, 4 and 5 were never exercised.** `push`, `re-review` and
  `merge-brief` still have no live evidence of any kind. Everything known
  about them comes from tests against fakes and real-git fixtures.
* **Two reviewer invocations per target is a small sample.** The divergence
  reported above is a real observation but not a measurement of anything.
* **The reviewer's read-only behaviour was probed, not enforced.** As in
  experiments #1 and #2.
* **One of the two targets was written by the operator**, and its result is
  reported here mainly as a negative case.
* **The authoring agent's fix was not itself independently reviewed.** It was
  checked by the operator against the finding, not re-reviewed by the loop —
  which is precisely the gap this document describes.

## Conclusion

The loop produced its first genuinely useful live result: a real Major
finding on a substantial pull request written by someone else, which the
pull request's author acted on, improving test coverage and correcting a
documentation overclaim in an authentication path.

It also showed two things worth carrying forward. A recorded finding can be
right about the code and wrong in its evidence, so the operator step between
recording and routing is not ceremonial. And two invocations of the same
reviewer on the same commit can find entirely disjoint problems, which means
one review is a sample and a dry run is not a preview.

The supported workflow's reach is narrower than the stage list suggests.
Round 2 is reachable only for a fix that workflow pushed itself. When the
ordinary thing happens — someone reads the comment and fixes it themselves —
the loop has nothing further to offer through the CLI beyond another round 1.
That is a bound on the workflow, not a provenance property; the handoffs
remain operator-controlled documents.

## Next recommendation

**Run stages 3 to 5 live, with the fix routed through `review-loop fix` and
`review-loop push` rather than done by the pull request's author.** That is
the only path to round 2, and it is now known to require arranging the
experiment that way from the start rather than hoping to reach it.

Concretely that needs: a pull request with a routable finding; an
`--agent-command` configured with write access, unlike the reviewer; and an
explicit decision to let `review-loop push` write a commit to that pull
request's branch. The last is a real repository write and is a human's call.
