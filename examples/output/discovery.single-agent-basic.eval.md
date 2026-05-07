# Pack eval: discovery

Configuration: **single-agent-basic**

## Aggregate

| Metric | Value |
| --- | --- |
| Tasks                  | 2 |
| Completed              | 2 / 2 (100%) |
| Total tool calls       | 5 |
| Mean tool calls / task | 2.5 |
| Median tool calls      | 2.5 |
| Total duration (s)     | 33.30 |
| Total cost (USD)       | $0.0274 |

## Success criteria

| Criterion | Passes | Applicable | Pass rate |
| --- | --- | --- | --- |
| `max_results` | 1 | 1 | 100% |
| `max_runtime_minutes` | 2 | 2 | 100% |
| `min_rating` | 1 | 1 | 100% |
| `min_results` | 2 | 2 | 100% |
| `min_runtime_minutes` | 1 | 1 | 100% |
| `must_include_genre` | 2 | 2 | 100% |

## Per-task

| Task | Completed | Modes | Tool calls | Duration (s) | Cost (USD) |
| --- | --- | --- | --- | --- | --- |
| `discovery-mood-tense` | yes | none | 4 | 26.93 | $0.0195 |
| `analytical-runtime-rating` | yes | none | 1 | 6.37 | $0.0080 |
