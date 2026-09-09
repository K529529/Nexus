# Nexus V1 Skill authoring guide

Nexus Skills are bounded Markdown guidance documents. They help the existing model plan and
execute a task, but they are not executable extensions: a Skill cannot register Tools, grant
approval, alter policy, weaken the sandbox, or replace validation.

## Directory and document format

A repository Skill lives at:

```text
<workspace>/.nexus/skills/<skill-id>/SKILL.md
```

A user-global Skill lives at:

```text
~/.nexus/skills/<skill-id>/SKILL.md
```

Only immediate child directories and the exact filename `SKILL.md` are discovered. The Skill
ID must match `^[a-z][a-z0-9-]{0,63}$`, and the directory name must equal the metadata ID.

Each file starts with strict TOML front matter between `+++` delimiter lines:

```markdown
+++
id = "diagnose-api"
name = "Diagnose API"
description = "Diagnose API failures from reproducible evidence."
usage_scenario = "Use for failing HTTP endpoints and API regressions."
priority = 50
version = "1.0.0"
+++

## Execution Principles

Reproduce the failure and separate observations from hypotheses.

## Recommended Tools

Use bounded repository search, focused reads, and approved validation commands.

## Workflow

1. Reproduce the smallest failing request.
2. Trace the narrowest relevant path.
3. Validate the evidenced correction.

## Constraints

Do not broaden scope or bypass Nexus policy and sandbox boundaries.
```

The required metadata keys are `id`, `name`, `description`, `usage_scenario`, and `version`.
`priority` is the only optional key and defaults to `0`. Unknown keys are invalid. Version is
SemVer core only, such as `1.0.0`; prerelease and build suffixes are not accepted.

The body must contain exactly one occurrence of each shown level-two heading in that order,
with non-empty content in every section. Other level-two sections may follow `## Constraints`.
The complete file is limited to 262,144 bytes and its front matter, including delimiters, is
limited to 16,384 bytes.

## Progressive disclosure

Discovery reads only bounded front matter. It does not read, hash, cache, log, or send the body
to the model. The selection model receives the explicit task and the validated metadata
catalog only. After the model selects identities, Nexus resolves source precedence and lazily
loads only the winning selected bodies.

This means:

- unselected bodies are never loaded;
- a lower-precedence shadowed body is never loaded;
- selected files are revalidated between metadata scan and body load;
- a changed, missing, unreadable, unsafe, or malformed selected file fails closed.

## Sources, collisions, and precedence

Source precedence is fixed:

```text
repository > user_global > builtin
```

The metadata catalog includes every valid source variant. The model selects Skill IDs, not
paths or variants. Nexus applies precedence only after selection. Priority and version are
model-visible relevance signals but never override source precedence.

The same ID in different sources is a valid override set. Duplicate IDs within one source are
fatal. Discovery never scans recursively and never uses URLs, archives, registries, Git
repositories, environment-provided roots, or legacy `skills/` paths.

## Selection and configuration

Day 7 adds one setting:

```toml
[skills]
max_selected_skills = 2
```

The environment equivalent is `NEXUS_MAX_SELECTED_SKILLS`. The value is an integer from `0`
through `2`; booleans are invalid. `0` disables model selection and body loading. There is no
Day 7 CLI override.

For a non-empty catalog and a positive limit, Nexus makes one structured selection model call.
The model may select zero, one, or two IDs. A valid empty result is a normal no-match. Invalid
JSON/schema, unknown or duplicate IDs, and over-limit output fail without retry or fallback.
The complete selection prompt must fit the existing total model-input ceiling; Nexus does not
pre-rank or silently omit metadata.

## Context budget and authority

Selected bodies share the existing `max_model_input_tokens` ceiling and do not consume the
separate code-context budget. Bodies are atomic: Nexus never truncates, summarizes, merges, or
section-trims them. Under pressure, the lower-ranked selected Skill is removed whole first. If
the first selected Skill cannot coexist with mandatory context, context construction fails.

Skill content is untrusted task guidance. Any Tool mentioned in a Skill is only a
recommendation. The approved Plan, Tool Runtime, approval policy, command policy, workspace
containment, sandbox, and validation remain authoritative. Markdown commands and code blocks
are inert text and are never executed by the Skill subsystem itself.

## V1 exclusions

Nexus V1 Skills do not support executable hooks, Skill-provided Tools or Agents, scripts,
assets, nested references, remote marketplaces, downloads, updates, signatures, configurable
roots, YAML/JSON front matter, heuristic fallback selection, per-step reselection, database
persistence, or long-term memory.
