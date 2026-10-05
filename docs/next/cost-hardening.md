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

## Stable prefix with ephemeral runtime state

Failure pattern: the current Plan and execution-budget counter were appended to
first-system-message copies. Their changes invalidated the reusable prefix ahead
of the entire growing history. The first Flash baseline consumed 1,491,589 input
and 37,565 output tokens in one 40-step case (cache usage was not yet recorded).
At the published uncached Flash tariff this is approximately CNY 1.29; it is not a
verified invoice or evidence of zero cache hits.

The current request now keeps system/project/environment and unchanged history
stable, then appends one typed, ephemeral user message with Plan and runtime notes.
No new logical history, source-seq references, persistence, model call or control
branch is introduced. Compaction selects/protects logical groups, then includes
Plan in its summary request; task requests also include one-time guidance and the
execution window budget. All estimates/checks/usage calibration see the actual
projection. Provider reasoning fields and assistant/tool pairs remain intact.

This is a general prefix-caching improvement; no repository/case text is involved.
It follows provider guidance to put stable context before dynamic state. Cache
reuse is provider-controlled and may still be reduced by observation projection,
safety compaction, routing or cache eviction; live accounting must verify savings.

Regression evidence includes serialized mock API requests retaining the complete
previous logical prefix across Plan updates and clearing, real user messages that
quote snapshot markers, non-accumulating request notes, current Plan after fallback
and compaction, unchanged logical snapshot references, and protected recent groups.

Offline validation: 14 targeted prefix/Plan/budget tests passed, then the full suite
passed 448 tests (10 opt-in Docker tests skipped). Ruff and mypy passed. One earlier
full run saw the pre-existing Windows child-process exit probe fail intermittently;
the focused timeout/cancel tests and the subsequent full run passed without a
runtime cleanup change. This is retained as a test reliability limitation.
