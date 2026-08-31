# Level 3 Parallel Delegated Development — Experiment #2

This document records a delegated-development process experiment: running
two independent Coding Agent tasks in parallel from a shared starting
commit, then measuring what happens on the human side once both resulting
pull requests are ready for review. It is a process/methodology record, not
a design document for any shipped feature — the features the two tasks
produced (`AgentResponse`, `get_event`) are documented in
`docs/agent-contracts/domain-model.md` and `docs/setup/google-calendar.md`
respectively.

## Objective

Test whether running multiple delegated Coding Agent tasks in parallel
creates a human-side bottleneck — active review time, context-switch
("reorientation") cost, or clarification needs — once two completed PRs are
reviewed back-to-back. This is distinct from agent-side execution
capability, which the prior experiment already validated.

## Background: difference from Experiment #1

Experiment #1 (2026-08-28) ran Task A (`AgentRequest` domain schema,
[PR #17](https://github.com/takolab/local-agent-concierge/pull/17), branch
`codex/level3-agent-request`) and Task B (a Google Calendar API child span,
[PR #18](https://github.com/takolab/local-agent-concierge/pull/18), branch
`codex/level3-calendar-api-span`) in parallel, from a shared base commit,
in separate worktrees, with non-overlapping scope. Agent-side execution was
fully validated: both tasks reached a green-CI, PR-ready state with 0 human
clarifications and 0 escalations.

The intended protocol also called for holding both PRs unreviewed until
both were ready, then reviewing them back-to-back, specifically to measure
human-side parallel-review burden. That part was **not** followed in
practice: PR #17 merged 2026-08-28T15:03:50Z and PR #18 merged
2026-08-28T15:53:58Z, roughly 50 minutes apart — each was reviewed and
merged individually as it became ready, not held for a joint review. So
Experiment #1 validated agent-side parallel delegation, but never actually
produced the "two completed PRs reviewed together" scenario the human-side
question depends on.

Experiment #2 repeats the same shared-BASE_SHA / separate-worktree /
separate-branch / non-overlapping-scope protocol, with one explicit
correction: **do not review or merge either PR until both are ready.**

## Task A and Task B

### Task A — `AgentResponse` domain schema ([PR #19](https://github.com/takolab/local-agent-concierge/pull/19))

Add `AgentResponse` — the Milestone 7 response-schema counterpart to
`AgentRequest` — to `packages/agent-contracts`:

- immutable, validated domain representation
- JSON-compatible serialization (`agent_response_to_dict` / `_from_dict`)
- no runtime integration, no framework dependency
- branch `feat/agent-contracts-agent-response`, worktree
  `../local-agent-concierge-task-a-agent-response`

### Task B — Google Calendar MCP `get_event` ([PR #20](https://github.com/takolab/local-agent-concierge/pull/20))

Add a read-only `get_event(event_id)` tool to the Google Calendar MCP
service:

- fetch a single primary-calendar event by ID
- read-only, reuses the existing `Event` shape (no new response schema)
- bounded 404 handling (`ValueError`, not the raw Google error body)
- OpenTelemetry instrumentation via the existing `trace_calendar_api` helper
- no sensitive data (event content, the `event_id` argument itself) in span
  attributes
- branch `codex/level3-calendar-get-event`, worktree
  `../local-agent-concierge-task-b-get-event`

## BASE_SHA

Both tasks started from `17a38d3b703daec2345192477a751cd3ae97a0ed` — PR
#18's merge commit, i.e. Experiment #1's final state, which was also
`origin/master`'s tip at the moment Experiment #2 started (confirmed via
`git fetch` + `git log origin/master -1`; the task-provided SHA needed no
drift correction). [PR #19](https://github.com/takolab/local-agent-concierge/pull/19)
and [PR #20](https://github.com/takolab/local-agent-concierge/pull/20) were
opened one second apart (2026-08-31T12:23:39Z and 12:23:40Z) — consistent
with, though not itself proof of, a genuinely parallel start; PR-creation
time reflects when each branch was pushed, not necessarily the exact
moment each task began executing.

## Experiment protocol

- Task A and Task B started from the same BASE_SHA.
- Each task ran in its own `git worktree` (not just a different branch in a
  shared checkout).
- Separate branches.
- Non-overlapping file scope: Task A was confined to
  `packages/agent-contracts/src`, `packages/agent-contracts/tests`, and
  `docs/agent-contracts/domain-model.md`; Task B to
  `mcp/google-calendar/src`, `mcp/google-calendar/tests`, and
  `docs/setup/google-calendar.md`. Neither task's original delegated run
  touched a file outside its own list.
- Zero cross-task coordination: each task's session confirmed only that the
  sibling branch/worktree existed (needed to pick a consistent worktree
  naming convention), never read its content.
- New this round: neither PR was reviewed or merged until both reached
  ready state.

## Agent-side results (original delegated run)

Both tasks reached a PR-ready state without any human intervention during
the original run:

- **Task A**: 0 clarifications, 0 escalations, 0 unnecessary intervention,
  no scope expansion. Tests: 156 passed / 0 failed (up from 87 at
  BASE_SHA), 100% line coverage. CI: both `Python tests` and
  `Agent Contracts tests` workflows green.
- **Task B**: 0 clarifications, 0 escalations, 0 unnecessary intervention,
  no scope expansion. Tests: 52 passed. CI: `Python tests` green;
  `Agent Contracts tests` correctly did not run (that workflow is
  path-filtered to `packages/agent-contracts/**`, which Task B never
  touched).

These figures are for each PR's initial commit only (`d604d1f` for PR #19,
`8315a5b` for PR #20) — see "Post-review changes" below for what happened
afterward.

## Human review results

Both PRs were held unmerged until both were ready this time. PR #19 merged
2026-08-31T16:14:07Z and PR #20 merged 2026-08-31T16:14:35Z — 28 seconds
apart, in immediate succession, unlike Experiment #1's ~50-minute gap. The
protocol correction held.

The timing and rating figures below are the reviewer's own account of a
silent reading process; GitHub has no record of how long a human spent
reading a diff before commenting, so these are self-reported as part of the
experiment record, not something pulled from GitHub. They are given in the
reviewer's local time, inferred to be UTC+1: every commit in this review
window is recorded with a consistent `+0100` offset (e.g. `git log` shows
PR #20's merge at `2026-08-31 17:14:34 +0100` against the GitHub API's
`2026-08-31T16:14:35Z`). Every other timestamp in this document is UTC, as
returned by the GitHub API. Normalized to UTC: PR #19's window is
12:56–13:01, PR #20's is 13:05–13:09 — i.e. both self-reported human scans
precede both independent-AI-review comments below (13:48–13:49 UTC).

**PR #19**
- Review window: 13:56–14:01 (5 min active review time)
- Confidence: 2/5 · Review difficulty: 4/5
- Clarification needed / changes requested during the initial scan / manual
  verification needed: no, no, no
- Reviewer notes: skimmed the diff but didn't know what needed to be
  checked, or how thoroughly, for the review to count as complete; even
  though the PR description called out review points, couldn't judge how
  far into the implementation to dig; it was unclear what was actually
  expected of the reviewer in this process. No issue was found, but
  approval confidence stayed low regardless.

**PR #20**
- Review window: 14:05–14:09 (4 min active review time)
- Confidence: 2/5 · Review difficulty: 3/5
- Clarification needed / changes requested during the initial scan / manual
  verification needed: no, no, no
- Reviewer notes: same ambiguity as PR #19 — unclear how deep to review or
  what the reviewer's responsibility boundary was.

**Important interpretation.** The combined 9 minutes of active review time
across both PRs should not be read as "two PRs reviewed successfully in 9
minutes." Both PRs scored the same low confidence (2/5) despite a clean
scan that found nothing — the correct reading is *a short scan found
nothing, but that did not translate into review confidence*, which is a
materially weaker result.

## Reorientation result

Not usefully measured. The plan was to measure the context-switch cost of
moving from PR #19's domain-schema review to PR #20's API-integration/
tracing review. That measurement presumes "ready to review" is already a
well-defined state for the reviewer to switch *into* — and per the review
notes above, it wasn't: the reviewer was already unsure what reviewing PR
#19 itself required, before ever reaching PR #20. So the bottleneck this
run surfaced sits earlier in the pipeline than technical context-switching
between a domain-schema task and an API-integration task.

## Independent AI review results

After the reviewer's own scan of each PR, each PR was separately run
through an independent ChatGPT technical review, with the resulting
findings posted into the PR conversation. (Every PR comment in this
repository — from the human's own review and from a Coding Agent's
in-session replies alike — is posted under the same GitHub account, since
sessions authenticate as the repository owner; GitHub's comment metadata
cannot itself distinguish "written by the human" from "drafted by an AI
tool and posted by the human." The independent-review framing below is
part of the experiment record as reported by the human. The comment text,
timestamps, and resulting code changes are independently verifiable on
GitHub.)

**PR #19.** The independent review identified a real composition defect the
human's 5-minute scan had not caught (comments at 2026-08-31T13:48:21Z and
13:48:32Z): `AgentResponse.proposed_actions` / `memory_candidates` accepted
only flat, non-empty-string-valued mappings, but
`packages/approvals.ProposedAction.action_to_dict()` — already present in
the repository — produces a nullable `target_event_id` and a nested
`parameters` mapping. The natural composition
`AgentResponse(proposed_actions=[action_to_dict(action)])` would therefore
fail validation. A second, explicitly non-blocking observation questioned
whether the same string-only constraint was the right provisional shape
for `memory_candidates`.

Both were addressed together in commit `cf3960a` (14:06:51Z): entry values
were loosened to any recursively JSON-compatible type
(`str`/`int`/`float`/`bool`/`None`, or nested `list`/mapping), entry *keys*
remain non-empty strings, and no dependency on `packages/approvals` was
introduced — `ProposedAction`'s real serialized shape now fits as one valid
"opaque JSON" entry without `agent-contracts` importing or adopting it. The
actual dependency-direction question (should `agent-contracts` depend on
`approvals`, the reverse, or neither) remains open, logged in
`docs/agent-contracts/domain-model.md`. 186/186 tests passed after the fix
(100% line coverage), and the PR description was updated to match
(16:08:10Z) after a final housekeeping request from the reviewer.

**PR #20.** The independent review found no blocking issues. It raised two
non-blocking follow-ups (comment at 13:49:38Z): (1)
`docs/observability/google-calendar-mcp-telemetry.md` was now stale (still
describing 6 tools / 2 `google_calendar.operation` values instead of 7 /
3), and (2) a suggestion to add a regression test confirming the MCP SDK's
own `tools/call get_event` *parent* span never carries the `event_id`
argument, in addition to the `google-calendar.api` child-span test the PR
already had.

**What this run showed, and what it didn't.** Across these two PRs, the
independent AI review surfaced one genuine, verified contract-composition
defect the human's short scan missed, and accurate, correctly-labeled
non-blocking suggestions on the other. That is one data point in favor of
this kind of review being useful for checking a new type's composition
against the rest of the domain model. It is **not** evidence that an AI
reviewer can replace human review, and should not be read as such pending
more observations across more PRs.

## Post-review changes

Kept separate from the original delegated-run results above, since both
PRs were revised only after review, not as part of the initial autonomous
run:

- **PR #19** — commit `cf3960a` (the entry-value typing fix described
  above) plus a PR-description update (16:08:10Z) requested as a
  housekeeping item. Both stayed inside the task's original Allowed Scope
  (`packages/agent-contracts/src`, `packages/agent-contracts/tests`,
  `docs/agent-contracts/domain-model.md`) — this was a defect fix, not a
  scope expansion.
- **PR #20** — commit `af5eff9` (2026-08-31T15:55:48Z) implemented both
  non-blocking follow-ups above, but only after the human explicitly
  instructed implementing them rather than leaving them as tracked
  follow-ups (which was the initial, correct default handling for
  non-blocking review comments). This touched
  `docs/observability/google-calendar-mcp-telemetry.md`, a file the
  original task brief's Allowed Scope had explicitly excluded — a genuine
  post-review scope expansion, done at explicit human request, not a
  unilateral agent decision. Final state: the telemetry doc now describes
  7 known tools and 3 `google_calendar.operation` values, and two new
  tests (`test_get_event_tool_call_span_omits_event_id_argument`,
  `test_get_event_not_found_tool_call_span_omits_event_id`) pin the parent
  span's omission of `event_id` on both the success and not-found paths.
  54/54 tests passing (local and CI-equivalent Docker) after this commit.

## What worked

- Agent-side parallel delegation itself: both tasks independently reached a
  clean, green, reviewable PR state, with zero escalations and zero
  unnecessary intervention — confirming Experiment #1's finding holds under
  a second, differently-shaped task pair.
- The protocol correction: holding both PRs unmerged until both were ready
  produced a genuine back-to-back review (28 seconds apart between merges),
  unlike Experiment #1's ~50-minute gap.
- Independent AI technical review caught a real, verified contract-
  composition defect that a short human scan did not, on this one data
  point.

## What did not validate

- The original hypothesis under test — parallel Coding Agents creating a
  human-side context-switching/reorientation bottleneck — was not actually
  tested, because a more basic bottleneck sat in front of it. Reorientation
  cost specifically could not be measured this run.
- Two PRs reviewed in 9 minutes combined is not evidence of efficient
  review: both carried confidence 2/5, meaning the reviewer did not trust
  their own scan either time.

## Main finding

The original hypothesis was that adding more parallel Coding Agents
increases human-side context-switching/review workload until it becomes
the bottleneck. This experiment did not get far enough to test that — a
more basic problem sat in front of it. In this experiment, low review
confidence coincided with the reviewer lacking a clear model of what had
been delegated and what they were responsible for verifying. This is an
observation from one reviewer across two PRs, not a general causal
relationship (see "Limitations / confounders").

Concretely, the human in this experiment tends to skim the long delegation
prompts an AI assistant drafts before handing them to a Coding Agent, so
"what was actually delegated" is only vaguely understood going into review.
That produces a loop with no reliable check in it: the AI drafts a detailed
prompt, the human passes it along without fully internalizing it, the
Coding Agent executes it faithfully, and the human then has no clear basis
for judging whether the resulting PR is correct.

## Decision — Delegation Summary

Going forward, any detailed Coding Agent delegation prompt written for this
project opens with a short, human-facing **Delegation Summary** covering
exactly five items: **Goal**, **Key design decision**, **Scope**, **Main
risk / uncertainty**, **Done when**. The human reads this before delegation
starts and needs to be able to say "I understand this well enough to hand
it off" — full understanding of the detailed implementation prompt is not
required, only genuine understanding and approval of these five items.

The same five items double as the PR review checklist: confirm the PR
matches the stated Goal, didn't silently change the Key design decision,
didn't exceed Scope, didn't make the Main risk worse, and satisfies Done
when — then spend code-level inspection effort only on the security/
architecture/data-handling areas that actually carry risk, rather than
requiring line-by-line understanding of the entire diff.

## Next experiment

Test whether the Delegation Summary actually improves human review
confidence, not just review speed. At minimum, record:

- **Before delegation**: a 1–5 human-understanding rating, plus whether the
  human can state — even briefly — what's being built, the key design
  decision, the allowed scope, the main risk, and the done condition.
- **After PR completion**: a 1–5 human review-confidence rating, and
  optionally active review time, review difficulty, AI technical review
  findings, changes requested, and manual verification requirements — the
  same shape of data collected in this experiment.

**Primary question:** is higher pre-delegation human understanding
associated with higher post-PR review confidence, improving on Experiment
#2's baseline (2/5 confidence on both PRs despite a clean, issue-free
scan)? Treat this as evidence accumulating across delegated tasks over
time, not a single pass/fail test of the Delegation Summary.

### Independent AI reviewer

The Coding Agent → independent AI reviewer → human decision flow looks
like a useful signal from this run, but it is one experiment. No dedicated
PR-review agent is being built yet. For now, keep using an ad hoc
independent reviewer (e.g. ChatGPT) across more PRs and observe what makes
a good review input, a good review checklist, a useful output format, and
what should actually be escalated to the human — before specifying anything
dedicated.

## Limitations / confounders

- **Single-reviewer, two-PR observation.** This experiment observed one
  reviewer's reaction to two PRs. The relationship this document describes
  between delegation-time understanding and PR-time review confidence
  ("Main finding") is an observation from that one pairing, not a general
  causal relationship established across reviewers or a larger PR sample.
- **PR size difference.** Task A (PR #19: 186 tests, a pure schema/
  validation package) and Task B (PR #20: 54 tests, an API-integration-
  plus-tracing feature) were not equivalent in size or shape. The
  review-difficulty difference (4/5 vs. 3/5) cannot be attributed to
  context-switching alone when the underlying review surfaces already
  differed.
- **Human review protocol was not defined in advance.** No agreed-upon
  "what counts as done reviewing this PR" existed before review started —
  the reviewer's own notes name this directly as the source of low
  confidence. Because of this, the Reorientation measurement this
  experiment was actually designed to collect could not be captured.
- **The independent AI review is one data point.** It caught a real,
  verified issue on PR #19 and produced accurate, appropriately-labeled
  non-blocking feedback on PR #20 — evidence it is useful, not proof it
  generalizes.
- **Review authorship on GitHub is not independently distinguishable.**
  Every PR comment in this repository is posted under the same GitHub
  account regardless of whether the text came from the human's own review
  or an AI tool's output relayed by the human. The "independent ChatGPT
  review" framing in this document is part of the experiment record as
  reported by the human, not something derivable from GitHub metadata
  alone.
- **Self-reported review timing.** Active-review-time and confidence
  figures are the human's own account of a silent reading process GitHub
  does not log; they were not cross-validated against, and do not
  necessarily align to the minute with, the PR comment timestamps cited
  elsewhere in this document.

## Relevant PR links

- Experiment #1: [PR #17](https://github.com/takolab/local-agent-concierge/pull/17)
  (`AgentRequest`), [PR #18](https://github.com/takolab/local-agent-concierge/pull/18)
  (Google Calendar API child span)
- Experiment #2: [PR #19](https://github.com/takolab/local-agent-concierge/pull/19)
  (`AgentResponse`), [PR #20](https://github.com/takolab/local-agent-concierge/pull/20)
  (`get_event`)
