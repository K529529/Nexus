# Day 7 Skill System implementation plan

**Authority:** Nexus V1.1.1 frozen baseline and
`docs/spec-addenda/Nexus_Day7_Contract_Addendum_v0.1.1.md`
**Branch:** `feature/day07-skill-system`
**Status:** APPROVED BY USER
**Implementation:** LOCAL CANDIDATE COMPLETE / AWAITING USER REVIEW

This plan translates the approved Day 7 contract into ordered implementation and
verification work. It does not modify or reinterpret any frozen public type, interface,
error, configuration, selection, precedence, security, graph, or budget contract.

Implementation was authorized by the user after the final Phase 5 wording correction.

---

## 1. Outcome and scope

The implementation will add a file-based Skill System that:

1. scans bounded TOML front matter without reading Skill bodies;
2. asks the existing `ModelGateway` to select zero, one, or two Skill identities;
3. resolves selected identities using `repository > user_global > builtin`;
4. lazily loads only the resolved selected bodies;
5. injects retained selected Skills into the existing bounded `WorkingContext`;
6. reuses the same selected context for Planner, replan/repair, and `agent_step`;
7. reports selected IDs and a safe reason through the existing `ContextBuilt` event;
8. proves unselected and shadowed bodies never enter prompts or body-load calls.

Day 7 will not add a graph node, Agent, Tool, MCP capability, CLI command, database schema,
runtime dependency, remote marketplace, executable Skill payload, long-term memory, or
Day 8+ functionality.

---

## 2. Frozen implementation invariants

The following are review blockers, not implementation choices:

- public domain values and signatures exactly match Addendum §§3 and 6;
- `SkillSelector.select()` receives the established `run_id: str` and calls
  `begin_model(run_id)` exactly once immediately before each real selection model call;
- disabled/empty-catalog selection performs no ledger increment and no model call;
- selector depends on `ModelInputBudgetGuard`, not concrete `BoundedContextManager`;
- the guard reuses Day 5 `model_input_tokens()` and the same resolved
  `max_model_input_tokens` value used by `BoundedContextManager`;
- selection budget failure occurs before ledger accounting/model invocation and never
  truncates metadata;
- Skill body load happens only after model selection and precedence resolution;
- Skill bodies are atomic and never truncated or summarized;
- Day 5 code/observation/conversation/manifest reduction order remains unchanged;
- the first selected Skill budget failure is `SKILL_CONTEXT_BUDGET_EXCEEDED`;
- no raw selection response or chain-of-thought is persisted or emitted;
- concrete construction remains in `src/nexus/infrastructure/bootstrap/`;
- all existing Day 1–6 behavior remains compatible when Skills are disabled or absent.

---

## 3. Current integration seams to preserve

Implementation will extend, not replace, these existing seams:

| Existing seam | Day 7 use |
|---|---|
| `ContextBuildRequest.run_id` | passed unchanged into `SkillSelector.select(run_id=...)` |
| `ManagedContextBuilder.build()` | coordinates scan, selection, resolution, lazy load, and context attachment |
| `WorkingContext` | receives only the two approved trailing defaulted fields |
| `context_payload()` | serializes exactly the approved selected-Skill fields |
| `BoundedContextManager.fit_model_input()` | performs existing reductions plus the approved atomic-Skill step |
| `prepare_agent_context()` | preserves selected Skills while refreshing history/observations |
| `ModelGateway.complete()` | performs structured Skill selection through the existing provider boundary |
| `ToolExecutionLedger.begin_model()` | accounts the selection call for the supplied Run |
| `ContextBuilt` | receives approved additive selected-ID/reason fields |
| Composition Root | constructs and injects all concrete Day 7 services |

No graph edge or node responsibility changes.

---

## 4. Planned file inventory

Exact private helper placement may be adjusted during implementation only when it does not
change a frozen contract. The expected narrow diff is:

### 4.1 New production files

```text
src/nexus/domain/skills.py
src/nexus/domain/ports/skills.py
src/nexus/domain/ports/model_input_budget.py
src/nexus/skills/__init__.py
src/nexus/skills/builtin/__init__.py
src/nexus/skills/loader.py
src/nexus/skills/registry.py
src/nexus/skills/selector.py
src/nexus/context/budget.py
src/nexus/skills/builtin/debug-python/SKILL.md
src/nexus/skills/builtin/write-tests/SKILL.md
src/nexus/skills/builtin/review-repository/SKILL.md
```

### 4.2 Existing production files expected to change

```text
src/nexus/domain/exploration.py
src/nexus/domain/runtime_events.py
src/nexus/domain/ports/__init__.py
src/nexus/config/models.py
src/nexus/config/loader.py
src/nexus/context/manager.py
src/nexus/context/integration.py
src/nexus/application/planning.py
src/nexus/infrastructure/graph/day4_runtime.py
src/nexus/infrastructure/bootstrap/composition.py
pyproject.toml                         # package-data inclusion only if wheel proof requires it
```

`application/planning.py` may change only to preserve the approved Skill authority wrapper
in the shared prompt path. `day4_runtime.py` may change only to populate the two additive
`ContextBuilt` fields; it must not alter topology, routing, counters, or node ownership.

### 4.3 Planned tests and documents

```text
tests/unit/test_day7_skill_domain.py
tests/unit/test_day7_skill_loader.py
tests/unit/test_day7_skill_registry.py
tests/unit/test_day7_skill_selector.py
tests/unit/test_day7_skill_context.py
tests/unit/test_day7_skill_config.py
tests/integration/test_day7_skill_graph.py
tests/integration/test_day7_skill_package.py
docs/skill-guide.md
docs/day7-acceptance-evidence.md
```

Test filenames are private organization details. Coverage must match the approved matrix even
if existing repository conventions justify combining files.

No Alembic migration or `uv.lock` dependency change is planned.

---

## 5. Ordered implementation phases

Each phase ends with focused tests before the next phase starts. A public-contract mismatch
stops work immediately.

### Phase 0 — Start gate and inherited-state audit

Before implementation:

1. confirm this plan has explicit user approval;
2. confirm branch remains `feature/day07-skill-system`;
3. inspect `git status`, branch/base SHAs, and inherited untracked changes;
4. reread the approved Addendum and relevant V1.1.1 sections;
5. confirm no newer approved contract or overlapping Day 7 implementation exists;
6. record the pre-implementation file/status baseline.

Stop if the branch contains unexplained production/test changes or if the approved Addendum
has been superseded.

### Phase 1 — Domain values, ports, and configuration surface

Implement the frozen values in `domain/skills.py`:

- `SkillSource`;
- `SkillLocation`;
- `SkillMetadata`;
- `SelectedSkill`;
- `SkillSelectionResult`;
- exact field validation, defaults, length/range rules, ID/path/version invariants, and
  immutable/slotted behavior.

Implement the frozen ports:

- `ModelInputBudgetGuard.ensure_fits()`;
- `SkillLoader.load_metadata/load_body()`;
- `SkillRegistry.scan_metadata/resolve_selected/load_selected()`;
- `SkillSelector.select(*, run_id: str, task: str, available_skills: Sequence[SkillMetadata])`.

Extend `RuntimeConfig` and loader with only:

```text
max_selected_skills: int = 2
NEXUS_MAX_SELECTED_SKILLS
[skills].max_selected_skills
```

Preserve range `0..2`, boolean rejection, per-field precedence, no CLI override, fixed roots,
and existing unknown-key behavior. Export only approved ports/values using current package
conventions.

Focused evidence:

- domain boundary/default/invalid-value tests;
- exact public signature/type tests;
- configuration default, zero, boundaries, invalid values, and precedence tests;
- existing config tests remain green.

### Phase 2 — Model-input budget guard

Create the minimal concrete guard behind `ModelInputBudgetGuard`:

1. accept the resolved `max_model_input_tokens` at construction;
2. call the existing Day 5 `model_input_tokens(messages)` without copying its algorithm;
3. return `None` when the rendered messages fit;
4. raise `ContextError` with the caller-provided code when they exceed the ceiling;
5. perform no message mutation, reduction, logging, ledger call, or model call.

Do not make the selector import or type against `BoundedContextManager`. Do not add a new
tokenizer, estimator, configurable ceiling, or reduction policy.

Focused evidence:

- exact below/equal/above-boundary tests;
- caller-supplied error-code preservation;
- input identity/content remains unchanged;
- a spy/monkeypatch proves the Day 5 accounting function is reused;
- static/import test proves no selector dependency on concrete ContextManager.

### Phase 3 — Metadata-only loader and source containment

Implement bounded source adapters using Python standard library only:

1. fixed roots for repository, user global, and builtin package resources;
2. immediate-child discovery only;
3. canonical root/document containment and regular-file checks;
4. 262,144-byte document and 16,384-byte front-matter bounds;
5. optional BOM, UTF-8 decoding, exact `+++` delimiters, and `tomllib` parsing;
6. exact required/optional/unknown-key handling;
7. metadata construction with derived source/location;
8. private stat/front-matter integrity snapshot without reading body bytes.

`load_body()` will revalidate containment, file bounds, stat/front matter, and metadata
equality before it reads and normalizes the selected body. It will then validate the four
exact ordered H2 sections and construct no executable behavior.

Focused evidence:

- an instrumented reader proves metadata scan stops at the closing delimiter;
- body marker bytes are never observed during metadata scan;
- malformed TOML/delimiters/fields/version/body map to exact error codes;
- invalid UTF-8, size limits, mutation between phases, and transient body I/O are mapped
  exactly;
- path traversal, absolute escape, symlink/junction/reparse escape, and special files fail
  closed;
- missing roots are empty/nonfatal and valid in-root documents succeed;
- builtin resources load from an installed wheel fixture.

Platform-specific link/junction cases may be skipped only when the platform cannot create
the fixture; the portable containment cases must always run.

### Phase 4 — Registry ordering, duplicate policy, and lazy resolution

Implement deterministic registry behavior:

1. scan sources in `repository, user_global, builtin` order;
2. sort by normalized relative path within each source;
3. return every valid metadata variant and no body;
4. fail same-source duplicate IDs before selection;
5. allow cross-source identity collision;
6. preserve selected-ID order;
7. resolve each selected ID after selection using source precedence;
8. lazily load only the winning selected variant exactly once.

Focused evidence uses call-recording fake loaders to prove:

- repository overrides user/builtin and user overrides builtin;
- priority/version never defeat source precedence;
- unknown selected ID and same-source duplicate fail with approved codes;
- unselected, losing, and shadowed bodies have zero `load_body()` calls;
- result ordering exactly matches selection order.

### Phase 5 — Structured model selector and per-Run accounting

Implement the ModelGateway-backed selector with this exact operation order:

```text
use established run_id, explicit task, and RuntimeConfig-validated maximum unchanged;
validate catalog
-> deterministically render metadata-only selection messages
-> ModelInputBudgetGuard.ensure_fits(..., SKILL_SELECTION_BUDGET_EXCEEDED)
-> begin_model(run_id)
-> ModelGateway.complete(messages)
-> parse and validate exact two-key JSON
-> return SkillSelectionResult
```

For disabled selection or an empty catalog:

```text
return deterministic empty SkillSelectionResult
do not invoke guard
do not invoke begin_model
do not invoke ModelGateway
```

The prompt includes only the approved task/metadata/maximum fields. The response validator
rejects unknown/extra/missing keys, unknown/duplicate IDs, invalid types/counts, empty or
overlong summaries, and invalid JSON. It has no retry or fallback. Provider/config failures
retain existing error types; normalized structured failures use
`SKILL_SELECTION_FAILED`.

Focused evidence:

- exact rendered metadata-only request and deterministic ordering;
- no path/body/repository/history/credential leakage;
- zero/one/two/no-match outputs;
- every malformed-output branch;
- budget failure occurs before ledger/model call;
- successful and failed provider calls each increment the supplied Run once;
- interleaved Run IDs remain independently accounted;
- no-call paths increment neither Run.

### Phase 6 — WorkingContext, payload, and budget integration

Add the two approved trailing defaults to `WorkingContext`:

```text
selected_skills = ()
skill_selection_result = None
```

Extend `context_payload()` with only `selected_skills`, containing only:

```text
skill_id, name, version, source, selected_reason, body
```

Update the existing authority wrapper so Skill body is explicitly task guidance below Nexus
safety, the user's task, applicable repository instructions, and the approved Plan.

Extend `BoundedContextManager.fit_model_input()` only at the approved Day 5 step 5:

1. keep the existing code reduction unchanged;
2. keep observation reduction unchanged;
3. keep conversation reduction unchanged;
4. keep manifest/layout reduction unchanged;
5. keep existing compacted optional reduction, then remove lower selected Skills whole from
   the end while retaining the first selected Skill;
6. raise `SKILL_CONTEXT_BUDGET_EXCEEDED` if the first selected Skill still cannot fit;
7. preserve existing `CONTEXT_BUILD_FAILED` behavior when no Skill was selected.

No Skill body is truncated, summarized, merged, copied into retrieval candidates, or charged
to the code budget.

Focused evidence:

- all existing positional WorkingContext constructors remain compatible;
- exact model-visible selected-Skill field set;
- lower selected Skill whole-removal order;
- first-Skill atomic failure;
- code/observation/conversation/manifest ordering regression tests;
- `prepare_agent_context()` preserves Skill fields across history/observation refresh;
- unselected metadata/body remains absent from final Planner/Agent messages.

### Phase 7 — Build-context coordination, Composition Root, and safe event

Extend `ManagedContextBuilder.build()` without changing the graph:

```text
registry.scan_metadata()
-> selector.select(run_id=request.run_id, task=request.task, available_skills=metadata)
-> registry.load_selected() (internally resolve precedence, then load)
-> existing ContextManager.build()
-> attach selected Skills and selection result
-> existing prepare_agent_context()/final fit
-> return WorkingContext
```

Construct in Composition Root:

- filesystem/package loader(s);
- `SkillRegistry`;
- concrete `ModelInputBudgetGuard` with the resolved Day 5 maximum;
- selector using the existing ModelGateway and `ledger.begin_model` hook;
- context integration using those abstractions.

Pass the exact same resolved `max_model_input_tokens` to guard and
`BoundedContextManager`. Do not construct Skill services in CLI, graph nodes, Planner,
Agent adapter, or ContextManager.

Extend `ContextBuilt` and its existing emission with only:

```text
selected_skill_ids
skill_selection_reason_summary
```

Populate IDs from Skills actually retained after budgeting. Emit only the validated safe
summary; never emit bodies, paths, catalog, raw response, or hidden reasoning.

Focused evidence:

- Composition Root object-graph test proves shared gateway/ledger/configured ceiling;
- no concrete ContextManager dependency in selector;
- `ContextBuildRequest.run_id` reaches exact per-Run accounting;
- existing graph node/edge snapshot remains unchanged;
- initial Plan, replan/repair, and agent_step see the same selected context;
- checkpoint resume after build_context does not reselect;
- absent/disabled/no-match Skill paths preserve Day 1–6 behavior;
- event payload contains safe IDs/reason only.

### Phase 8 — Demo Skills, guide, and end-to-end acceptance fixtures

Add the three approved builtin Skills using the exact v0.1.1 TOML/body contract:

- `debug-python`;
- `write-tests`;
- `review-repository`.

Each Skill is reviewable context guidance only and must not contain an executable hook,
provider configuration, credential, policy bypass, or unbounded instruction.

Add `docs/skill-guide.md` covering:

- directory and front-matter format;
- metadata versus body and progressive disclosure;
- source roots, collision, and precedence;
- selection/no-match/failure behavior;
- context budget and atomic body handling;
- authority/security boundary;
- local repository Skill authoring examples;
- explicit V1 exclusions.

End-to-end fixtures use a deterministic fake `ModelGateway` and instrumented loader to prove
the three task scenarios, repository override, selected workflow influence, and absence of
unselected/shadowed bodies. No real-provider test is required by the approved contract.

### Phase 9 — Regression, packaging, and acceptance evidence

After focused tests pass:

1. run all existing Day 4 context/coding-loop tests;
2. run all Day 5 context/retrieval/budget tests;
3. run all Day 6 MCP graph/tool visibility tests;
4. run the complete non-local suite;
5. run Ruff, Mypy, lock check, and diff check;
6. build/install the wheel in an isolated test environment and prove builtin `SKILL.md`
   resources are present and readable;
7. inspect the final diff for scope, secrets, accidental body loading, graph changes, and
   dependency/lock churn;
8. write `docs/day7-acceptance-evidence.md` with local results and explicit unverified gates.

Do not commit, push, open/update a PR, merge, or rebase unless separately requested.

---

## 6. Test and acceptance matrix

| Frozen contract | Primary planned evidence |
|---|---|
| domain fields, defaults, validation | `test_day7_skill_domain.py` |
| metadata-only bounded scan | instrumented loader tests |
| exact TOML/front-matter/body format | valid/invalid loader matrix |
| fixed roots and path containment | filesystem, symlink/junction, and resource tests |
| same-source duplicate/cross-source override | registry unit tests |
| post-selection precedence | registry + graph integration tests |
| structured selection and no fallback | selector fake-gateway matrix |
| no-match/disabled/empty catalog | zero-call selector and integration tests |
| `run_id` per-Run accounting | success/failure/interleaved ledger tests |
| selection-prompt budget seam | guard boundary/order/import tests |
| selected-only lazy body load | call-recording loader tests |
| exact model-visible Skill fields | `context_payload()` assertions |
| atomic Skill budget handling | Day 5 reduction regression + Day 7 pressure tests |
| Planner/replan/agent_step reuse | graph integration test |
| safe `ContextBuilt` reporting | event payload/redaction assertions |
| repository Skill override | deterministic repository integration fixture |
| three demo workflow effects | fake-gateway end-to-end scenarios |
| builtin packaging | built-wheel resource test |
| Day 1–6 compatibility | focused predecessor suites + full suite |

---

## 7. Planned verification commands

Use the restricted-Windows-safe project cache:

```powershell
uv --cache-dir .uv-cache run --frozen pytest tests/unit/test_day7_skill_domain.py tests/unit/test_day7_skill_loader.py tests/unit/test_day7_skill_registry.py tests/unit/test_day7_skill_selector.py tests/unit/test_day7_skill_context.py tests/unit/test_day7_skill_config.py
uv --cache-dir .uv-cache run --frozen pytest tests/integration/test_day7_skill_graph.py tests/integration/test_day7_skill_package.py
uv --cache-dir .uv-cache run --frozen pytest tests/unit/test_day4_graph.py tests/unit/test_day5_context.py tests/integration/test_day6_mcp_graph.py
uv --cache-dir .uv-cache run --frozen ruff check .
uv --cache-dir .uv-cache run --frozen mypy src tests
uv --cache-dir .uv-cache lock --check
uv --cache-dir .uv-cache run --frozen pytest --ignore=tests/local
git diff --check
git status --short
```

If private test-file organization changes, use the equivalent exact paths without reducing
coverage. `tests/local` remains outside CI and outside Day 7 acceptance.

No database migration, live embedding, or real MCP server is required specifically for Day 7.
Existing integration tests may still require their documented infrastructure; skips and
environment failures must be reported separately from passes.

---

## 8. Review checkpoints

Implementation should be presented for review at these boundaries:

1. **Contract skeleton:** domain values, ports, config, and tests compile; no loader/model
   behavior yet.
2. **Progressive disclosure:** loader/registry tests prove metadata-only scan and selected-only
   body loading.
3. **Selection boundary:** exact structured response, budget guard, and per-Run accounting
   pass.
4. **Context integration:** WorkingContext, budget order, Planner/Agent reuse, and event safety
   pass without graph changes.
5. **Acceptance candidate:** demos, guide, packaging, full local gates, and final diff audit
   complete.

No checkpoint authorizes merge. Any requested architecture correction must be reflected in an
approved Addendum revision before code is changed across a frozen boundary.

---

## 9. Stop conditions

Stop implementation and request an approved contract revision if any of these occurs:

- metadata-only validation cannot be implemented without reading body bytes;
- TOML/package resource support requires a new runtime dependency;
- safe source containment requires changing WorkspaceGuard/security policy;
- selection requires a new ModelGateway method or provider-specific API;
- per-Run accounting cannot reuse `begin_model(run_id)` without changing ledger semantics;
- selector would need concrete `BoundedContextManager` knowledge;
- metadata catalog pressure would require pre-ranking, truncation, or fallback;
- Skill injection would require a graph node/edge or public Planner/Agent contract change;
- Day 5 eviction order or existing mandatory-context semantics would need reinterpretation;
- event reporting would require raw model output, body content, or private reasoning;
- wheel packaging would require a dependency or non-approved Skill bundle format;
- any database, CLI, Tool, MCP, Agent, security-risk, or Day 8+ change appears necessary.

Private helper names, test fixture implementation, standard-library mechanics, and equivalent
low-level structure remain within the implementation decision boundary.

---

## 10. Explicitly deferred work

- remote marketplace/download/update/signing;
- YAML/JSON compatibility front matter;
- recursive Skill bundles, scripts, assets, templates, or references;
- executable hooks or Skill-provided Tools/Agents/MCP/providers;
- heuristic/fallback selection, metadata pre-ranking, retry, or per-step reselection;
- configurable Skill roots or additional Skill settings;
- database persistence and long-term memory;
- Day 8 observability redesign and Day 9 evaluation framework;
- commits, pushes, PR/CI operations, merge, and release work without separate authorization.

---

## 11. Plan approval gate

```text
Day 7 Contract Addendum v0.1.1: APPROVED BY USER
Day 7 Implementation Plan:      APPROVED BY USER
Production implementation:      IMPLEMENTED LOCALLY / AWAITING USER REVIEW
Test implementation:            IMPLEMENTED LOCALLY / AWAITING USER REVIEW
Dependency changes:              NOT AUTHORIZED
Commit / push / PR / merge:      NOT AUTHORIZED
```

Plan approval authorizes only the frozen Day 7 scope; it does not authorize contract changes,
future-Day work, Git delivery operations, or merge.
