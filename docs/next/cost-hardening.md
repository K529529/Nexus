# Cost hardening evidence

## Optional provider cache accounting

Failure pattern: the model adapter kept aggregate input/output usage but discarded
`prompt_tokens_details.cached_tokens`. Long tool trajectories therefore could not
distinguish expensive uncached input from discounted repeated context. This affects
all tasks, not a repository or evaluation case.

The adapter now records `cached_input_tokens` as an optional subset of input usage.
Missing/invalid counts remain unknown; zero is a measured zero. JSONL, run totals and
profile exports preserve this distinction and include retry/compaction attempts.
No inference decisions, tool permissions or evaluation checks change. Prices stay
outside the runtime because provider tariffs and cache-write charges vary.

Validation: 98 model/profile/evaluation export regression tests passed, including
SDK streaming parsing, old events without cache fields, zero/invalid values and an
empty-response retry. Ruff and mypy passed. The already-running Flash baseline was
started before this change and consequently has no cache-use measurements.

Sources:
- [Bailian prefix caching](https://platform.qianwenai.com/docs/developer-guides/run-and-scale/context-cache)
- [Qwen3.8-Flash pricing](https://www.qianwenai.com/models/qwen3.8-flash)
