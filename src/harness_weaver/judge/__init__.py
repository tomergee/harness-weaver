"""Judge: structural diagnostics + LLM-as-judge over Trajectories.

Two layers, used together by ``harness-weaver compare``:

1. **Structural** (``classifier.py``, ``structural.py``): rules-based,
   deterministic, free. Tags failure modes (hallucinated tool, infinite
   loop, off-task, refusal, cost blow-up) and produces a side-by-side
   markdown comparison of two trajectories. Always runs.

2. **LLM judge** (``llm.py``): asks Claude (via inspect-ai) to compare
   the trajectories on a fixed rubric and emit a JSON verdict. Costs
   money; opt-in.

The structural report is fed into the LLM judge's prompt as
pre-computed scaffolding, so the model gets the counts and reasons
about them rather than re-counting from raw events.
"""

from harness_weaver.judge.classifier import FailureMode, classify
from harness_weaver.judge.llm import (
    DEFAULT_JUDGE_MODEL,
    FixedJudge,
    InspectAILlmJudge,
    Judge,
    JudgeVerdict,
)
from harness_weaver.judge.structural import (
    StructuralReport,
    TrajectorySummary,
    render_markdown,
)

__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "FailureMode",
    "FixedJudge",
    "InspectAILlmJudge",
    "Judge",
    "JudgeVerdict",
    "StructuralReport",
    "TrajectorySummary",
    "classify",
    "render_markdown",
]
