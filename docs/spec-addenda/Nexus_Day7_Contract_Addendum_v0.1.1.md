# Nexus Day 7 Contract Addendum v0.1.1

**Addendum Version:** `v0.1.1`
**Applies To:** `Nexus V1 Product Requirements & 10-Day Engineering Specification v1.1.1`
**Milestone:** Day 7 — Skill System
**Implementation Branch:** `feature/day07-skill-system`
**Status:** APPROVED CONTRACT — IMPLEMENTATION AUTHORIZED
**Authority:** User-approved Day 7 implementation contract

This document is the frozen Day 7 implementation contract. Its original approval authorized
implementation planning only; the user subsequently approved the corrected Implementation
Plan and separately authorized production and test implementation within this contract.

This contract-only v0.1.1 revision adds the per-Run `SkillSelector.select(run_id=...)`
accounting contract and the Nexus-owned `ModelInputBudgetGuard` seam. All other v0.1
contracts remain unchanged.

---

## 1. Authority and scope

The parent Specification remains authoritative. In particular, this Addendum must be read
with sections 3, 4, 7, 9.1, 9.3, 11, 12, and the complete Day 7 specification.

Day 7 adds file-based, task-specialized context guidance. It does not add an Agent, Tool,
MCP server/client capability, executable payload, graph node, remote marketplace, database
schema, or long-term memory facility.

The following remain frozen:

- the existing Agent graph topology and node responsibilities;
- `ModelGateway` as the sole model-provider boundary;
- `ContextManager` ownership of complete model-input budgeting;
- Planner, ToolRuntime, ApprovalPolicy, CommandPolicy, and Sandbox authority;
- `Repository > User Global > Builtin` Skill source precedence;
- `max_selected_skills=2` as the V1 default;
- no raw or inferred private chain-of-thought in state, events, logs, or output.

If implementation requires changing a contract below, Codex must stop, show evidence and
impact, present options, and await an approved revision.

---

## 2. Audited Day 1–6 integration baseline

This draft was prepared against repository tree `dbecc3a5ea914e1fca24c5495e69dd2c5e644a49`,
whose file content matches `origin/main` at
`ce5d238da0b8dbf6160401620f98e12b973e24b0`. The local `main` reference is stale, but no
content divergence exists between the audited Day 7 working tree and the merged Day 6 tree.

The following existing contracts constrain Day 7:

1. `WorkingContext` is a frozen, slotted dataclass in `domain/exploration.py`; Day 5 already
   reserved future Skill integration as additive-only.
2. `context_payload()` is the single shared model-visible representation used by initial
   planning, replan/repair planning, and every `agent_step`.
3. `BoundedContextManager.fit_model_input()` owns WorkingContext reduction and the sole Day 5
   eviction order; the Day 7 selection prompt uses the Nexus-owned budget-guard seam with the
   same Day 5 accounting function and resolved 24,000-token ceiling.
4. `ManagedContextBuilder` is the compatibility seam between the existing `build_context`
   graph node and `ContextManager`.
5. `ModelGateway.complete()` accepts Nexus-owned `ModelMessage` values and returns a
   normalized `ModelResponse`; Skill selection must use that port.
6. the existing `ContextBuilt` event can be extended additively with defaulted Skill fields;
   no new graph node or event family is required.
7. concrete dependency assembly is centralized in
   `src/nexus/infrastructure/bootstrap/composition.py`.
8. `RuntimeConfig` is frozen and resolved per field with the parent precedence contract.

No Day 7 production/test implementation exists in the audited tree.

---

## 3. Frozen Day 7 domain values

The following are the frozen public Day 7 types. Exact module placement may follow the
existing repository layout, but their fields and semantics must not change during
implementation without approval.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SkillSource(StrEnum):
    REPOSITORY = "repository"
    USER_GLOBAL = "user_global"
    BUILTIN = "builtin"


@dataclass(frozen=True, slots=True)
class SkillLocation:
    source: SkillSource
    relative_path: str


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    skill_id: str
    name: str
    description: str
    usage_scenario: str
    source: SkillSource
    priority: int
    version: str
    location: SkillLocation


@dataclass(frozen=True, slots=True)
class SelectedSkill:
    metadata: SkillMetadata
    body: str
    selected_reason: str


@dataclass(frozen=True, slots=True)
class SkillSelectionResult:
    selected_skill_ids: tuple[str, ...]
    selection_reason_summary: str
```

### 3.1 Field invariants

`SkillSource` values have this descending precedence and no other V1 value:

```text
repository > user_global > builtin
```

`SkillLocation.relative_path`:

- is source-root-relative, normalized with `/`;
- has the exact form `<skill_id>/SKILL.md`;
- is never absolute and contains no empty, `.`, or `..` component;
- is derived by the loader and is never accepted from front matter or model output.

`SkillMetadata`:

- `skill_id` matches `^[a-z][a-z0-9-]{0,63}$`;
- `name` is trimmed, non-empty, and at most 100 Unicode code points;
- `description` is trimmed, non-empty, and at most 500 Unicode code points;
- `usage_scenario` is trimmed, non-empty, and at most 500 Unicode code points;
- `source` and `location` are loader-derived and cannot be authored in `SKILL.md`;
- `priority` is an integer in `0..100`, with default `0`; booleans are invalid;
- `version` is an exact SemVer core string `MAJOR.MINOR.PATCH`; each component is a
  non-negative decimal integer without leading zeroes except `0`; prerelease and build
  suffixes are not accepted in V1;
- `location.source == source` and the directory component of `location.relative_path`
  equals `skill_id`.

`SelectedSkill`:

- contains exactly one resolved metadata variant and its lazily loaded body;
- `body` excludes front matter, uses `\n` line endings, is non-empty, and ends in one `\n`;
- `selected_reason` is the safe `selection_reason_summary` returned by the selector; it is
  not chain-of-thought and does not grant authority.

`SkillSelectionResult`:

- preserves model-returned Skill ID order; the first ID is the most relevant;
- contains unique IDs only;
- contains at most the configured `max_selected_skills`;
- may contain `selected_skill_ids=()` as a normal no-match result;
- has a trimmed, non-empty summary of at most 1,000 Unicode code points, including for
  no-match results;
- never contains raw model output, hidden reasoning, provider objects, or prompt content.

---

## 4. `SKILL.md` document contract

### 4.1 Location and encoding

Each Skill is one directory containing one file:

```text
<skill-root>/<skill-id>/SKILL.md
```

Only immediate child directories of a Skill root are scanned. Nested discovery, alternate
filenames, multiple documents per directory, and remote documents are out of scope.

The file must be UTF-8, with an optional UTF-8 BOM. Decoding failure is invalid metadata
during metadata scan or a body-load failure if the file changed after scan. The V1 fixed
maximum is 262,144 bytes per complete `SKILL.md`; it is not configurable. This bound prevents
an unselected document from becoming an unbounded local parsing input and does not reserve
model-input budget. The front matter, including delimiters, has a separate fixed maximum of
16,384 bytes. Metadata scan stops after the closing delimiter and never reads beyond that
delimiter merely to validate, hash, cache, or inspect the body.

### 4.2 Front matter syntax

V1 uses TOML front matter so the format can be parsed by Python 3.12 `tomllib` without a new
runtime dependency. The first non-BOM bytes must be an opening delimiter line `+++`; the next
delimiter line containing only `+++` closes front matter. Content between the delimiters is
one TOML document. A missing/extra delimiter or invalid TOML is invalid.

Example:

```markdown
+++
id = "debug-python"
name = "Debug Python"
description = "Diagnose Python failures from reproducible evidence."
usage_scenario = "Use for Python exceptions, failing tests, and behavioral regressions."
priority = 60
version = "1.0.0"
+++

## Execution Principles

Reproduce first and separate evidence from hypotheses.

## Recommended Tools

Use repository search, focused file reads, and the approved validation tools.

## Workflow

1. Reproduce the failure.
2. Trace the smallest relevant path.
3. Validate the narrow correction.

## Constraints

Do not broaden scope or bypass policy and sandbox boundaries.
```

The required front matter keys are exactly:

```text
id
name
description
usage_scenario
version
```

The only optional key is:

```text
priority = 0
```

Unknown keys are invalid. In particular, `source`, filesystem paths, commands, executable
hooks, environment variables, provider configuration, tools, and code payloads are not
front matter metadata.

### 4.3 Body syntax and required sections

The body is Markdown after the closing delimiter. It must contain exactly one occurrence of
each required level-two heading, in this order:

```text
## Execution Principles
## Recommended Tools
## Workflow
## Constraints
```

Each required section must contain non-whitespace content. Other level-two sections may
follow `## Constraints`; they remain ordinary guidance and receive no special authority.
Heading matching is case-sensitive and exact in V1.

Execution principles, recommended tools, workflow, and constraints belong only to the body.
They must not be copied into `SkillMetadata` or read during metadata scan. Tool names in a
body are recommendations only: they do not register a Tool, alter Tool metadata, authorize a
Plan action, or bypass ToolRuntime/policy/sandbox checks. Markdown code blocks and command
examples are inert text and are never executed by the Skill subsystem.

### 4.4 Metadata/body integrity between phases

`load_metadata()` may record a private stat snapshot and a SHA-256 fingerprint of the parsed
front matter bytes only; it must not read or fingerprint the body. `load_body()` revalidates
containment, regular-file status, fixed size bounds, the current front matter, and its equality
with the selected `SkillMetadata` before returning body text. A changed stat snapshot or
metadata mismatch fails safely with `SKILL_BODY_LOAD_FAILED`; the loader does not combine
stale selection metadata with a changed document or silently select again.

---

## 5. Skill sources, discovery, containment, and precedence

### 5.1 Fixed V1 roots

```text
Repository:  <canonical workspace root>/.nexus/skills/
User Global: <Path.home()>/.nexus/skills/
Builtin:     package resources under nexus.skills.builtin/<skill-id>/SKILL.md
```

The repository and user roots are fixed; Day 7 adds no configurable path. Builtins must be
packaged as importable resources and loaded through `importlib.resources`, not by assuming a
source-checkout-relative path.

A missing root is an empty source, not an error. A root that exists but is not a readable
directory is `SKILL_PATH_UNSAFE` or `SKILL_METADATA_INVALID`, as applicable. V1 does not scan
legacy `skills/`, environment-provided paths, URLs, archives, package registries, or Git
repositories.

### 5.2 Path safety

For filesystem-backed sources, the loader must:

1. canonicalize the configured root once;
2. enumerate only immediate child directories;
3. resolve the candidate directory and `SKILL.md` strictly;
4. verify the resolved document remains below the resolved source root using path-component
   containment, never string-prefix comparison;
5. reject an absolute/relative escape, a symlink/junction/reparse-point escape, or a special
   non-regular file with `SKILL_PATH_UNSAFE`;
6. never follow links merely to discover additional Skills.

An internal link whose resolved regular file remains under the same canonical root is
allowed. Builtin resources must satisfy the equivalent package-resource containment and may
not resolve to external filesystem/network content.

Path safety is independent of Skill contents and may not be weakened by front matter.

### 5.3 Identity collision and precedence

Identity collision uses the validated, case-sensitive `skill_id`. Because IDs are restricted
to lowercase ASCII, no platform-specific case-folding rule is needed.

- two documents with the same ID in one source are a fatal `SKILL_ID_DUPLICATE`;
- the same ID in different sources is a valid override set, not an error;
- priority never overrides source precedence;
- version never overrides source precedence;
- the highest-precedence available variant is the only variant whose body may be loaded.

Per the frozen parent control flow, cross-source precedence is resolved **after model
selection**, not before. Metadata scan returns every valid source variant. The selection
prompt groups variants by `skill_id` and exposes each variant's name, description,
usage_scenario, source, priority, and version. The model returns identities only. For every
selected identity, `SkillRegistry` then resolves exactly one variant in this order:

```text
repository, else user_global, else builtin
```

No unselected identity is resolved for body loading, and no lower-precedence body is loaded.

---

## 6. Frozen public interfaces

All file/model I/O interfaces are async. Private implementation helpers may use synchronous
standard-library parsing behind these async boundaries, but no synchronous provider or
filesystem interface is exposed to Agent/graph code.

```python
from collections.abc import Sequence
from typing import Protocol

from nexus.domain.model import ModelMessage


class ModelInputBudgetGuard(Protocol):
    def ensure_fits(
        self,
        messages: Sequence[ModelMessage],
        *,
        error_code: str,
    ) -> None: ...


class SkillLoader(Protocol):
    async def load_metadata(self, location: SkillLocation) -> SkillMetadata: ...

    async def load_body(self, metadata: SkillMetadata) -> str: ...


class SkillRegistry(Protocol):
    async def scan_metadata(self) -> tuple[SkillMetadata, ...]: ...

    def resolve_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SkillMetadata, ...]: ...

    async def load_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SelectedSkill, ...]: ...


class SkillSelector(Protocol):
    async def select(
        self,
        *,
        run_id: str,
        task: str,
        available_skills: Sequence[SkillMetadata],
    ) -> SkillSelectionResult: ...
```

### 6.1 `ModelInputBudgetGuard` behavior

- it is a minimal Nexus-owned port for validating an already-rendered model request;
- `ensure_fits()` is synchronous because it performs pure in-memory accounting and no I/O;
- it uses the existing Day 5 `model_input_tokens(messages)` function without a second
  estimator and compares against the same resolved `RuntimeConfig.max_model_input_tokens`
  value supplied to `BoundedContextManager`;
- it returns `None` when the complete serialized request fits;
- when the request exceeds the ceiling, it raises `ContextError` with the caller-supplied
  stable `error_code` and does not mutate, truncate, summarize, or reorder messages;
- it owns no selection, context reduction, provider, graph, or ledger behavior.

### 6.2 `SkillLoader` behavior

- `load_metadata()` may read only the bounded prefix necessary to find and parse front
  matter. It must not read, parse, return, cache, hash, log, or expose body text.
- `load_body()` is called only for metadata returned by `resolve_selected()`.
- direct calls with an unsafe location or metadata/location mismatch fail; the loader does
  not repair or reinterpret them.
- loader failures use the Day 7 `ContextError` codes in section 11.

### 6.3 `SkillRegistry` behavior

- `scan_metadata()` scans sources in deterministic order
  `repository, user_global, builtin`, then normalized relative path order;
- it returns every valid source variant and no body;
- it detects same-source duplicates before selection;
- `resolve_selected()` validates every selected ID exists, preserves selected ID order, and
  applies source precedence;
- `load_selected()` is equivalent to `resolve_selected()` followed by ordered
  `load_body()` calls and construction of `SelectedSkill` values;
- it must never load an unselected or shadowed body's content, even speculatively.

### 6.4 `SkillSelector` behavior

- the concrete selector receives `ModelGateway`, `max_selected_skills`, the existing model
  call ledger/counter hook, and `ModelInputBudgetGuard` from the Composition Root;
- the public `select()` call does not expose provider configuration;
- `run_id` is the non-empty canonical Run UUID already established by `AgentState`; the
  caller passes it unchanged, and the selector must not generate, replace, or normalize it;
- when `max_selected_skills == 0` or `available_skills` is empty, it returns a deterministic
  empty result without calling the model;
- otherwise it makes exactly one `ModelGateway.complete()` call;
- immediately before that call it invokes the existing ledger's `begin_model(run_id)`, so the
  attempted call increments `llm_call_count` exactly once for that Run, including a
  provider/model failure;
- it has no heuristic, priority-only, keyword, first-Skill, or builtin fallback.

---

## 7. Model-based structured selection

### 7.1 Model-visible selection input

The selection request contains only:

- a Nexus system instruction defining the exact output schema and authority boundary;
- the current explicit task;
- metadata fields `skill_id`, `name`, `description`, `usage_scenario`, `source`, `priority`,
  and `version`, grouped by identity in deterministic catalog order;
- the configured `max_selected_skills`.

It must not contain any Skill body, Skill path, repository file contents, conversation,
observation, credential, raw provider object, or private fingerprint.

Task relevance is primary. `priority` is a model-visible tie-break signal among relevant
Skills; it does not force selection. `source` informs selection but only the registry applies
the frozen override rule.

### 7.2 Exact output schema

The model must return one JSON object with exactly these keys:

```json
{
  "selected_skill_ids": ["debug-python"],
  "selection_reason_summary": "The task asks for evidence-driven Python debugging."
}
```

Validation is exact:

- `selected_skill_ids` is a JSON array of unique strings in catalog identity set;
- array order is meaningful and length is `<= max_selected_skills`;
- `[]` is valid and requires a safe non-empty no-match summary;
- `selection_reason_summary` satisfies section 3.1;
- unknown/missing/extra keys, unknown IDs, duplicates, wrong types, over-limit results,
  empty output, invalid JSON, or an overlong/empty summary are
  `SKILL_SELECTION_FAILED`.

Provider/config/transport failures remain the existing `ModelError` or
`ConfigurationError`. Only structurally invalid normalized selection output becomes
`ContextError(code="SKILL_SELECTION_FAILED", retryable=True)`.

The selector does not retry or fallback internally. Any future retry policy requires an
approved contract because it changes model-call counts and runtime behavior.

### 7.3 Selection prompt budget

The actual serialized selection messages, including roles, JSON escaping, and conservative
framing overhead, must fit the existing `max_model_input_tokens`. Before ledger accounting or
the ModelGateway call, the selector invokes:

```python
model_input_budget_guard.ensure_fits(
    selection_messages,
    error_code="SKILL_SELECTION_BUDGET_EXCEEDED",
)
```

The selector depends only on the Nexus-owned `ModelInputBudgetGuard` port, never on concrete
`BoundedContextManager`. The guard reuses the Day 5 `model_input_tokens()` calculation and the
same resolved `max_model_input_tokens` value used by `BoundedContextManager`; the selector
must not implement another estimator or silently slice metadata.

If the complete metadata catalog does not fit, selection fails deterministically with
`SKILL_SELECTION_BUDGET_EXCEEDED`. V1 does not invent a metadata pre-ranking or silently omit
Skills, because either would be a new selection algorithm. This error occurs before the
ModelGateway call and therefore does not increment `llm_call_count`.

---

## 8. WorkingContext and graph integration

### 8.1 Additive WorkingContext fields

Day 7 adds only these trailing, defaulted fields:

```python
@dataclass(frozen=True, slots=True)
class WorkingContext:
    # Existing Day 4/5 fields remain unchanged and in their current order.
    selected_skills: tuple[SelectedSkill, ...] = ()
    skill_selection_result: SkillSelectionResult | None = None
```

Existing constructors and consumers remain source-compatible. In the Day 7 path,
`skill_selection_result` is always non-null after selection, including no-match. Legacy test
fixtures and the Day 1 interruption path may retain the default `None`.

`AgentState.context` remains the authoritative checkpointed WorkingContext. No separate
Skill graph state, business table, or persistence owner is introduced.

### 8.2 Model-visible WorkingContext representation

`context_payload()` adds exactly one key:

```text
selected_skills
```

Each item contains:

```text
skill_id
name
version
source
selected_reason
body
```

Description, usage scenario, priority, path, fingerprints, shadowed variants, unselected
metadata, the raw selection response, and `skill_selection_result` are not injected into
Planner/Agent prompts. An empty/no-match selection serializes as `selected_skills=[]`.

Skill content is wrapped as task guidance below Nexus safety, the user's explicit task,
applicable repository instructions, and the approved Plan. It cannot change tool risk,
authorization scope, command policy, sandbox containment, validation requirements, or graph
routing. Instructions in a Skill that attempt to do so are inert and must be ignored.

### 8.3 Existing graph reuse

The graph remains:

```text
explore_repository -> build_context -> create_plan -> ... -> agent_step
```

The existing `ManagedContextBuilder` seam coordinates the Day 7 flow within
`build_context`:

```text
scan metadata only
-> structured model selection
-> resolve selected identities by source precedence
-> lazily load only resolved bodies
-> build existing repository/retrieval context
-> add selected Skill values to WorkingContext
-> run final ContextManager model-input fitting
-> return one WorkingContext
```

Initial planning, replan/repair planning, and every `agent_step` use the same
`context_payload()` and therefore the same selected Skills. `prepare_agent_context()` must
preserve the two new fields while refreshing observations/history and reapplying the final
budget. Day 7 performs selection once per `build_context` execution; it does not reselect on
each Agent step or replan.

Checkpoint resume reuses checkpointed selected Skills when resuming after `build_context`.
If execution restarts before `build_context`, normal graph semantics may run selection once.

### 8.4 Safe event and user-visible report

`ContextBuilt` is extended additively with defaulted fields:

```python
selected_skill_ids: tuple[str, ...] = ()
skill_selection_reason_summary: str | None = None
```

These fields report the Skills actually retained in the final bounded WorkingContext and the
validated safe summary. The event never includes a body, path, catalog, raw response, or
private reasoning. No new CLI command/argument is added. Existing event rendering may show
the IDs and safe summary as the Day 7 user-visible selected-Skill report.

---

## 9. Context budget and progressive disclosure

### 9.1 Frozen principles

- unselected and shadowed Skill bodies never enter memory caches, WorkingContext, prompts,
  token accounting callbacks, events, traces, or logs;
- selected body loading occurs only after identity selection and source resolution;
- every selected Skill body shares the existing total `max_model_input_tokens` ceiling;
- Skill content does not consume or change `max_code_context_tokens` or chunk-count limits;
- the existing `estimated_tokens = ceil(character_count / 4)` policy and complete serialized
  message accounting remain unchanged;
- Skill bodies are atomic and must not be text-truncated, summarized, section-trimmed, or
  merged, because partial constraints/workflows can reverse their meaning.

### 9.2 Deterministic retention order

Selected Skill order is the model-returned `selected_skill_ids` order after source
resolution. Earlier items have higher retention priority. If two selected bodies cannot both
fit after the already-frozen Day 5 reduction stages, the last selected Skill is removed whole
first. A removed Skill is not reported as selected in final WorkingContext or `ContextBuilt`.

The complete Day 5 reduction order is preserved and refined only at its existing step 5:

```text
1. remove lowest-priority retrieved code; explicit/task seeds last
2. compact/reduce older observations
3. compact/reduce conversation history
4. reduce non-authoritative manifest/layout evidence
5. reduce remaining optional context:
   a. existing compacted optional context by its frozen relevance/recency behavior
   b. remove selected Skills whole, from last selected toward first
6. fail safely if required context still cannot fit
```

At least the first selected Skill is an atomic Day 7 instruction pack once selection has
succeeded. If it cannot coexist with the frozen mandatory context under the hard ceiling,
the build fails with `SKILL_CONTEXT_BUDGET_EXCEEDED`; the body is not truncated and the run
does not falsely report that the Skill influenced workflow. When selection is empty, the
existing `CONTEXT_BUILD_FAILED` behavior for mandatory non-Skill context remains unchanged.

This is a Day 7 refinement at the Day 5-reserved future Skill integration point; it does not
change code ranking, code/observation/conversation ordering, ceilings, or authority.

### 9.3 Progressive-disclosure evidence

Tests must be able to instrument loader calls and final rendered messages to prove:

```text
all valid metadata may be scanned
selected resolved bodies are loaded exactly once
unselected bodies are loaded zero times
shadowed bodies are loaded zero times
only final retained selected bodies are model-visible
```

---

## 10. Configuration contract

Day 7 adds exactly one logical setting:

```toml
[skills]
max_selected_skills = 2
```

Environment binding:

```text
NEXUS_MAX_SELECTED_SKILLS
```

Validation:

- integer only; booleans are invalid;
- allowed V1 range is `0..2`;
- default and absolute V1 maximum are `2`;
- `0` explicitly disables selection and body loading without disabling other context.

No Day 7 CLI override is added. For sources that define the field, per-field precedence
remains:

```text
CLI (no Day 7 binding)
> repository .nexus/config.toml
> user ~/.nexus/config.toml
> environment
> defaults
```

The Skill roots are fixed by section 5 and are not configuration. No Skill secret exists.
Only `max_selected_skills` is recognized from `[skills]`. Other keys are ignored consistently
with the existing loader's unknown-field behavior and never become executable, provider, or
path configuration.

---

## 11. Structured error contract

Day 7 uses the existing `ContextError` hierarchy except ordinary RuntimeConfig validation,
which remains `ConfigurationError`.

| Code | Trigger | Fatality |
|---|---|---|
| `SKILL_METADATA_INVALID` | malformed delimiters/TOML, missing/unknown field, invalid text/priority, directory-ID mismatch, unreadable non-selected metadata, oversized file during scan | fatal before selection |
| `SKILL_VERSION_INVALID` | version is not the exact V1 SemVer core form | fatal before selection |
| `SKILL_ID_DUPLICATE` | more than one metadata document has the same ID in one source | fatal before selection |
| `SKILL_PATH_UNSAFE` | path/root/regular-file/containment/symlink rule fails | fatal; never skipped |
| `SKILL_BODY_LOAD_FAILED` | selected resolved document is missing, unreadable, changed after scan, or cannot be decoded | fatal after selection; `retryable=True` only for transient I/O |
| `SKILL_BODY_INVALID` | selected body is empty or violates required-section contract | fatal after selection |
| `SKILL_SELECTION_FAILED` | normalized model response violates the structured-selection contract | fatal; `retryable=True` |
| `SKILL_SELECTION_BUDGET_EXCEEDED` | complete metadata-only selection request exceeds total model-input ceiling | fatal before model call |
| `SKILL_CONTEXT_BUDGET_EXCEEDED` | first selected atomic body cannot fit with mandatory context after frozen reductions | fatal during final context assembly |

Non-error/recoverable continue cases are:

- missing source root;
- no discovered Skills;
- `max_selected_skills=0`;
- a valid `selected_skill_ids=[]` no-match;
- a cross-source collision resolved by precedence;
- removal of a lower-ranked selected Skill as one atomic unit under section 9.2.

Fatal metadata/path errors are not silently skipped merely because that Skill might not have
been selected. Silent skipping would make the selection catalog environment-dependent and
hide invalid repository/user configuration.

Every error message is safe and may include only source, Skill ID if validated, and normalized
relative path. It must not expose body content, complete prompt, home/workspace absolute path,
credentials, raw provider response, or private reasoning.

---

## 12. Composition Root and module boundaries

Concrete construction remains exclusively under `src/nexus/infrastructure/bootstrap/`:

```text
RuntimeConfig
-> ModelInputBudgetGuard using Day 5 model_input_tokens/max_model_input_tokens
-> filesystem/package SkillLoader(s)
-> SkillRegistry
-> ModelGateway-backed SkillSelector
-> ManagedContextBuilder / ContextManager integration
-> existing Planner + Agent + Graph + NexusRuntime
```

The same `ModelGateway` instance used by Planner/Agent is supplied to the selector. The same
`ToolExecutionLedger` is supplied for `begin_model(run_id)` accounting. The Composition Root
constructs one Nexus-owned `ModelInputBudgetGuard` with the existing Day 5
`model_input_tokens()` function and the exact same resolved
`RuntimeConfig.max_model_input_tokens` value passed to `BoundedContextManager`.

`BoundedContextManager` continues to own WorkingContext reduction/fitting. The guard owns only
the no-mutation fit check for model requests, so the selector has no concrete ContextManager
dependency and no second budgeting policy is introduced.

Allowed conceptual module placement:

```text
src/nexus/domain/skills.py
src/nexus/domain/ports/skills.py
src/nexus/domain/ports/model_input_budget.py
src/nexus/skills/loader.py
src/nexus/skills/registry.py
src/nexus/skills/selector.py
src/nexus/skills/builtin/<skill-id>/SKILL.md
src/nexus/context/integration.py
src/nexus/infrastructure/bootstrap/composition.py
```

Domain and ports must not import LangChain, concrete model providers, Typer, MCP, SQLAlchemy,
or package-specific YAML/front-matter libraries. Day 7 requires no database migration and no
new dependency. TOML parsing uses the Python standard library.

CLI handlers, graph nodes, Planner, Agent adapter, and ContextManager must not construct
loaders, registries, selectors, or provider clients. Skill content must not be registered in
`ToolRegistry` or supplied as MCP Tool metadata.

---

## 13. Required deterministic tests

### 13.1 Domain and format

- validate every field/default/boundary in section 3;
- accept exact valid TOML front matter and required body sections;
- reject malformed/missing/unknown front matter fields and delimiters;
- reject invalid version, priority boolean/out-of-range, directory-ID mismatch, invalid UTF-8,
  empty body, duplicate/misordered/missing required sections, and oversized document;
- prove metadata load returns no body and does not retain body text.

### 13.2 Sources and security

- repository overrides user and builtin only after selection;
- user overrides builtin;
- priority/version cannot defeat source precedence;
- same-source duplicate is fatal and cross-source collision is valid;
- discovery order is deterministic;
- missing root is empty/nonfatal;
- reject absolute traversal, `..`, symlink/junction escape, special file, and resolved path
  outside root;
- allow a regular in-root document and, where platform-supported, an internal in-root link;
- verify builtin package-resource loading works from the built wheel, not only source checkout.

### 13.3 Selection

- model sees task plus metadata fields only, never any body/path;
- `select()` receives the established Run ID and passes that exact value to
  `begin_model(run_id)` immediately before a real selection model call;
- valid one/two-Skill selection preserves order;
- `[]` no-match succeeds;
- `max_selected_skills=0` and empty catalog make zero model calls;
- every attempted selection model call increments only that Run's `llm_call_count` exactly
  once, including model failure;
- two interleaved Run IDs remain independently accounted with no cross-Run increment;
- invalid JSON/schema/type/extra key/duplicate/unknown ID/over-limit/summary fails with
  `SKILL_SELECTION_FAILED` and does not fallback;
- provider failure remains existing `ModelError`/`ConfigurationError`;
- oversized metadata selection request fails before `begin_model(run_id)` and the model call
  with the exact budget code.

### 13.4 Lazy body load and integrity

- only resolved selected bodies are loaded;
- unselected and shadowed bodies have zero body-read calls;
- selected body is loaded once;
- metadata/body file mutation is detected and no mixed-version SelectedSkill is built;
- body/read failure maps to the exact structured code.

### 13.5 WorkingContext and budget

- additive defaults preserve all Day 1–6 WorkingContext call sites;
- selected Skill representation reaches `context_payload()` with exactly the allowed fields;
- initial Planner, replan/repair Planner, and agent_step receive the same selected body;
- context refresh preserves selection while changing observation/history views;
- unselected metadata/body never enters final messages;
- two selected Skills retain model order and the second is removed whole first under pressure;
- no body is truncated or summarized;
- code, observations, conversation, and manifest/layout reduce in the existing Day 5 order;
- inability to retain the first selected body yields `SKILL_CONTEXT_BUDGET_EXCEEDED`;
- no-selection mandatory overflow remains `CONTEXT_BUILD_FAILED`.

### 13.6 Configuration and Composition Root

- default, repo, user, environment, and mixed per-field precedence;
- integer/range/boolean validation and explicit zero disablement;
- a `ModelInputBudgetGuard` unit test proves `ensure_fits()` uses Day 5
  `model_input_tokens()`, the same configured ceiling, caller-supplied code, and no mutation;
- boundary tests prove the selector accepts the guard port and has no import/type dependency
  on concrete `BoundedContextManager`;
- one shared ModelGateway, ledger, registry, selector, budget guard, and ContextManager are
  wired in the Composition Root;
- Composition Root tests prove guard and ContextManager receive the same resolved
  `max_model_input_tokens` value;
- no selector/provider construction occurs in CLI, graph, Planner, Agent, or ContextManager;
- legacy interrupt path and Skill-disabled path preserve Day 1–6 behavior.

### 13.7 Repository integration and demo Skills

Implementation must provide three reviewable demo Skills:

```text
debug-python
write-tests
review-repository
```

The integration fixture must prove:

1. repository `debug-python` overrides a same-ID builtin after model selection;
2. a Python debugging task selects and injects only `debug-python`;
3. a testing task selects and injects only `write-tests`;
4. a review task selects and injects only `review-repository`;
5. the resulting Planner workflow reflects the selected guidance;
6. unrelated and shadowed bodies are absent from the prompt and loader body-call log;
7. selected IDs and safe reason appear in `ContextBuilt`, with no body/event leakage.

Model selection tests use a deterministic fake `ModelGateway`. A real-provider selection is
not required to prove deterministic contract behavior unless separately approved as an
acceptance exercise.

---

## 14. Acceptance and quality gates

Day 7 implementation cannot be considered complete until all are separately evidenced:

1. **Acceptance Criteria:** the three demo scenarios prove metadata-first selection,
   post-selection precedence resolution, lazy selected-only loading, bounded injection, and
   visible safe reporting.
2. **Code & Architecture Review:** public types/signatures, source/path safety, ModelGateway
   usage, ContextManager ownership, additive graph integration, and no executable payload are
   reviewed against this approved Addendum.
3. **Tests / CI:** targeted Day 7 tests, full pytest suite, Ruff, Mypy, lock check, diff check,
   and repository CI pass; local skips are reported separately.
4. **Product Owner Knowledge Review:** the owner can explain progressive disclosure, source
   precedence, metadata versus body, selection failure/no-match, context-budget behavior, and
   why Skills cannot bypass Planner/ToolRuntime/security.

No merge occurs before all four gates and explicit review approval.

---

## 15. Explicitly deferred and forbidden scope

- remote Skill marketplace, download, update, publish, signing, or trust service;
- executable Skill hooks, shell/Python payload execution, dynamic imports, or plugin behavior;
- Skill-provided Tools, Agents, MCP servers, model providers, credentials, or configuration;
- graph topology/node changes, parallel Skill/model/tool execution, or per-step reselection;
- database persistence/schema, long-term memory, telemetry/trace redesign, or evaluation
  framework work from Day 8/9;
- recursive Skill discovery, nested bundles, attachments/assets, scripts, templates, or
  references outside the single `SKILL.md` V1 document;
- a second context budget, tokenizer, model client, policy engine, or fallback selector;
- arbitrary YAML/JSON front matter or compatibility aliases in V1.

---

## 16. Contract-gap disposition and approval record

The audit found no Frozen Baseline conflict requiring a redesign. It found these previously
unfrozen Day 7 public gaps, all resolved by this approved contract:

| Gap | Approved v0.1.1 resolution |
|---|---|
| exact metadata/domain fields | section 3 dataclasses and validation |
| front matter grammar/dependency | strict TOML `+++` front matter; standard library only |
| body section contract | four exact ordered required H2 sections |
| source roots and containment | fixed repo/user/package roots and canonical containment |
| duplicate/override timing | same-source fatal; cross-source precedence after selection |
| interface signatures | async loader/registry/selector contracts in section 6 |
| per-Run selector accounting | `select(run_id=...)` and exact `begin_model(run_id)` timing |
| selection prompt budget seam | minimal Nexus-owned `ModelInputBudgetGuard`; Day 5 estimator and shared ceiling |
| structured model response/fallback | exact two-field JSON; no fallback or hidden retry |
| selection-prompt budget | Nexus-owned `ModelInputBudgetGuard`; fail rather than silently prefilter |
| WorkingContext/model/event fields | additive fields and a single context payload path |
| Skill/body budget behavior | atomic body; lower selected Skills evicted last-to-first; first selected Skill budget failure |
| configuration | only `max_selected_skills`, range `0..2`, fixed roots |
| error codes/fatality | section 11 matrix |
| deterministic acceptance | section 13 test matrix and three demo Skills |

Approval of this document freezes those choices and authorizes preparation of a separate
Day 7 Implementation Plan. The user subsequently approved the corrected Implementation Plan
and separately authorized Day 7 production and test implementation under this frozen contract.

```text
Contract approval:      APPROVED BY USER
Implementation Plan:    APPROVED BY USER
Production code:        IMPLEMENTED LOCALLY / AWAITING USER REVIEW
Test code:              IMPLEMENTED LOCALLY / AWAITING USER REVIEW
Dependency changes:     NOT AUTHORIZED
```
