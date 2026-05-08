# ADR-0005: LLM-as-judge — rubric, structural scaffold, inspect-ai backbone

## Status

Accepted — 2026-05-08

## Context

The harness needs a way to answer "did configuration A do better than
configuration B on this task?" automatically. A purely rules-based
comparison can count tool calls and check `Task.success_criteria`, but
it can't tell whether an answer is *good* — whether it addresses what
the user asked, whether claims are grounded in tool results, whether
efficiency was reasonable for the question. That judgment is what an
LLM-as-judge is for.

Three design questions had to land before the judge could be built:

1. **What is the judge actually evaluating?** Without an explicit
   rubric, two judges (or the same judge across runs) drift to
   whatever subjective dimension feels salient — usually verbosity or
   surface polish, neither of which is what we care about.
2. **How does the judge access the trajectory?** Trajectories are
   long, mostly-mechanical event streams. Asking the LLM to count tool
   calls or detect an "early exit" by reading raw events is wasteful
   tokens and unreliable inference.
3. **What library/runtime drives the model call?** We could call the
   Anthropic SDK directly, but the harness already has the Claude
   Agent SDK in the loop for the agents under test, and binding the
   judge to the same SDK would tie evaluation to the thing being
   evaluated. We want the judge to be replaceable with a different
   provider without ripping out infrastructure.

## Decision

**A four-priority rubric, structural scaffold-as-prompt, and inspect-ai
as the model runtime.**

### 1. Rubric (priority order, ties break upward)

The judge evaluates on, in this order:

1. **Task fidelity.** Did the agent address what the user actually
   asked for? An eloquent answer to the wrong question loses to a
   plain answer to the right one.
2. **Grounding.** Are the claims in the final answer supported by
   tool results visible in the trajectory? Hallucinated titles,
   ratings, or runtimes are disqualifying.
3. **Tool use efficiency.** Did the agent reach its answer with
   reasonable tool calls, or thrash? Repeated identical searches and
   tool errors count against it.
4. **Final answer quality.** Clarity, justification grounded in
   catalog facts, fit to the user's stated mood/constraints.

The rubric is encoded in the system prompt as a numbered list with
that exact priority order; the model is instructed to break ties by
walking down the list. Output is JSON matching :class:`JudgeVerdict`:
`winner ∈ {a, b, tie, both_fail}`, `reasoning`, `confidence ∈ [0, 1]`.

### 2. Structural scaffold as prompt input

Before the model sees the trajectories, the judge runs
:meth:`StructuralReport.of` on both sides. The report carries:

* per-side event count, tool-call count, tool-error count
* wall-clock duration
* presence and length of a final answer
* failure-mode tags from :mod:`.classifier` (early-exit, runaway
  loop, tool-error-cascade, no-final-answer, etc.)
* success-criteria pass/fail when the `Task` carries them

That report is rendered to markdown via
:func:`render_markdown` and embedded in the user prompt as a "here
are the numbers, here are the failure-mode tags, now reason about
quality." The model never has to count events itself.

### 3. inspect-ai for the model call

The judge wraps :func:`inspect_ai.model.get_model` and uses
:class:`ChatMessageSystem` / :class:`ChatMessageUser` plus
``Model.generate``. Default model is
``anthropic/claude-haiku-4-5-20251001`` (cost). Override at
construction time for stronger judges.

The :class:`Judge` protocol has one async method, `verdict(*, task,
trajectory_a, trajectory_b) -> JudgeVerdict`, with two
implementations: :class:`InspectAILlmJudge` (production) and
:class:`FixedJudge` (canned verdict, used in tests and when wiring
without an API key).

## Consequences

**Easier:**

* **Determinism in the structural layer.** Event counting and
  failure-mode classification are pure functions of the trajectory.
  Even when the LLM verdict varies between runs, the scaffold doesn't
  — and the verdict ships the scaffold attached
  (`JudgeVerdict.structural`) so report consumers see exactly what
  numbers the LLM was reasoning over.
* **Cheap-to-run baseline.** The structural report alone is the
  default `compare` output; LLM verdicts are opt-in via
  `--judge-model`. A reviewer who wants only the rules-based diff
  pays nothing.
* **Provider portability.** inspect-ai supports Anthropic, OpenAI,
  AWS Bedrock, etc. Switching judge providers is a model-id change,
  not a rewrite.
* **Fakable for tests.** :class:`FixedJudge` lets us wire the CLI's
  verdict-rendering path end-to-end without an API key.

**Harder:**

* **Self-reported confidence is not calibrated.** We treat
  `confidence` as nothing more than the model's own opinion. Until a
  calibration set lands (~20-30 human-rated trajectory pairs with
  Cohen's-kappa-style agreement vs. judge), high confidence on
  individual verdicts means little. The README design notes flag this
  as future work; this ADR doesn't promise to solve it.
* **Rubric drift across model upgrades.** Newer models may
  re-interpret "task fidelity" subtly differently. The fix is the
  calibration set + a regression suite of sample trajectories with
  known verdicts; same future work as above.
* **Two-trajectory comparison only.** The current verdict shape is
  pairwise. Pack-level "config A beats config B on N of M tasks"
  aggregation is a separate layer (currently a future-work item) that
  rolls up per-task verdicts; it isn't this ADR.

## Alternatives rejected and why

* **No rubric, just "which one is better?"** Tried in a throwaway
  prototype; verdicts skewed toward whichever side had more prose,
  regardless of correctness. The four-priority rubric was the
  smallest intervention that produced verdicts aligned with the
  failure modes we care about (catalog hallucination, runaway loops,
  early exits).
* **Let the model count tool calls itself.** Burns tokens and is
  flaky on long trajectories. The structural scaffold is cheap,
  deterministic, and visibly correct on inspection — better
  separation of concerns.
* **Bind the judge to the Claude Agent SDK.** The harness already
  uses the SDK to drive the agents *under test*. Reusing it for the
  judge couples evaluation to the thing being evaluated; an SDK
  regression that also affects the judge would mask itself. Keeping
  judge transport on inspect-ai isolates the seam.
* **Call the Anthropic SDK directly.** Possible, but loses
  inspect-ai's provider abstraction (`anthropic/`, `openai/`,
  `bedrock/...` are all one method) and the eval-tooling adjacency
  (inspect-ai is built for evaluation; we get its retry, budget, and
  conversation-shape primitives for free).
* **Numerical 0-100 score per dimension instead of a winner.**
  Forces the model to fabricate granularity it doesn't have, and
  averaging dimension scores assumes commensurability we haven't
  shown. The discrete `{a, b, tie, both_fail}` shape is honest about
  what the judge can actually decide.

## Notes

* The system prompt is a string constant `_SYSTEM_PROMPT` in
  :mod:`harness_weaver.judge.llm`. Treat it as part of the rubric's
  contract — changes there change verdict distribution.
* `JudgeVerdict.structural` is `None` only when the judge is called
  outside the normal `compare` flow. The CLI always populates it.
* The structural-only path (`compare` without `--judge-model`) is the
  cheap default and is what CI tests exercise. The LLM judge path
  needs `ANTHROPIC_API_KEY` (or the equivalent for whichever provider
  inspect-ai resolves the model id against).

## References

* :mod:`harness_weaver.judge.llm` — `_SYSTEM_PROMPT`,
  :class:`InspectAILlmJudge`, :class:`FixedJudge`,
  :class:`JudgeVerdict`.
* :mod:`harness_weaver.judge.structural` —
  :class:`StructuralReport`, :func:`render_markdown`.
* :mod:`harness_weaver.judge.classifier` — failure-mode tagging.
* `inspect_ai.model.get_model`, `ChatMessageSystem`, `ChatMessageUser`.
* README §"Judge design" — the evaluation philosophy this ADR formalizes.
* ADR-0002 — orchestrator-worker via SDK subagents (the topology the
  judge evaluates).
