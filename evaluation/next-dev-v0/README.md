# Next Dev Set V0

Fixed local evaluation for eight coding tasks. It reports local validator results, **not official SWE-bench resolved results**. Context Runtime V0.1.1 is the current baseline; V0.2.1 has not been implemented.

From the Nexus checkout, with Docker Desktop running Linux containers and your existing model configuration/key environment variable:

```powershell
.\.venv\Scripts\nexus.exe eval pvlib__pvlib-python-1707
.\.venv\Scripts\nexus.exe eval --all
```

No additional `--profile` is needed. Ordinary interactive TUI behavior is unchanged. Evaluation prints one start/end line per case and one aggregate table including `FinalCtx` and `ToolResultBytes`. Full events and tool output remain in the session JSONL. User Ctrl+C stops the suite, saves the current result and marks unstarted cases `NOT_RUN`.

Results: `~/.nexus/evaluation/results/<run-id>/`. Each case has `result.json`, the collected `patch.diff`, its existing session JSONL, and independent validator logs/results. `manifest.json`, `summary.json`, and `report.txt` identify the exact implementation, model, fixed data, environment and observed metrics. Unknown usage stays unknown; partial totals are lower bounds. Exit codes: 0 all PASS, 1 FAIL, 2 ERROR/NOT_RUN, 130 interrupted.

## Fixed environments

Eight local image IDs in the case TOMLs are frozen after actual negative/positive qualification. See [qualification.json](qualification.json) and [acceptance evidence](../../docs/next/evaluation-v0-evidence.md). The current host already has all eight images and verified source caches.

The internal prepare step validates the image identity and each source blob/file mode, builds a fresh one-commit repository, and starts a fresh container. Tools run without network, user MCP configuration, model credentials, Docker socket or validator mounts. Model access stays on the host. Only the disposable project workspace is mounted into the Agent container. Preparation and validation time are excluded from Agent Profile duration.

Before exposing that container to the Agent, preparation removes target-project copies from all Python environments (including vendored packages and stubs), package/source caches, and archives containing the target. Matplotlib's exact-base generated files are first copied into `/workspace`, then the extra build copy is removed. Pytest is installed editable from `/workspace` after cleanup. A final filesystem/archive scan fails preparation on leftovers or unreadable scan inputs. This cleanup changes only the disposable container, not the frozen image or host cache; starting the raw image directly does **not** provide this isolation. See [contamination qualification](../../docs/next/evaluation-contamination-evidence.md). The earlier model baseline is retained as contaminated historical evidence, not a clean comparison baseline.

If a frozen local image is absent, preparation attempts its reviewed Dockerfile and then requires the original image ID. Rebuilding packages/layers on another machine may produce a different ID; that is a `setup_error`, not permission to silently change the experiment. Transfer the exact image with Docker save/load for same-condition comparisons. Dockerfiles record preparation recipes; they are not a claim of bit-identical cross-host builds.

Canonical source snapshots are cached under `~/.nexus/evaluation/cache/sources/`. Preparation verifies raw Git blob identities and modes, including archive export-subst differences and symlinks. It does not clone upstream history into the Agent workspace. Windows extraction and cleanup run inside Linux containers so Linux symlinks survive correctly. Existing tracked modes are normalized in the synthetic Git index to avoid NTFS permission emulation changing the baseline.

Validation starts from another fresh base, applies the candidate patch, restores declared hidden test paths and applies the fixed hidden tests. Missing/skip/setup-error required targets cannot PASS. Agent `completed` is not validation PASS, and `limited` can still produce a passing candidate. Candidate commits are collected against a control-side original index, so committing changes cannot hide them.

## Cases and sources

| Case | Source / fixed checks |
| --- | --- |
| pvlib__pvlib-python-1707 | SWE-bench Lite **dev**; IAM physical regression and retention |
| matplotlib__matplotlib-22835 | SWE-bench Lite test; BoundaryNorm cursor and artist retention |
| django__django-11734 | SWE-bench Verified test; queries/exclude/OuterRef, SQLite |
| psf__requests-6028 | SWE-bench Verified test; authentication URL regressions and utils retention |
| sphinx-doc__sphinx-10323 | SWE-bench Verified test; literalinclude ordering and retention |
| click__path-generic-type | [Click PR 3858](https://github.com/pallets/click/pull/3858); runtime and strict assert_type checks |
| pytest__doctest-optionflag-leak | [pytest PR 15033](https://github.com/pytest-dev/pytest/pull/15033); doctest same-process option-state regression and retention |
| rich__double-width-wrap | [Rich PR 3180](https://github.com/Textualize/rich/pull/3180); public Text.wrap character/cell-width regression and retention |

Benchmark test targets/test patches come from [SWE-bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) and [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified). Curated reference fixes are used only for control-side qualification. They are never put into Agent images or workspaces. Curated task descriptions and base commits are frozen in the case directories.

Two small support scripts capture actual pytest/unittest test identities during independent validation. They are fixed test reporters, not Agent plugins. Some original benchmark report labels abbreviate parametrized test IDs; their explicit one-to-many mappings are retained in `targets.json`, without dropping any original required target.

## Developer checks

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src tests

# Opt-in real Docker acceptance (no model calls):
$env:NEXUS_TEST_DOCKER='1'
.\.venv\Scripts\pytest.exe tests/test_evaluation_docker.py -q
Remove-Item Env:NEXUS_TEST_DOCKER
```

Compare `summary.json` by case ID after checking manifests. Keep tasks, validators, image IDs, model settings and budgets fixed. A future Context implementation may change Nexus source hashes and the Context policy label; other differences must be reported explicitly. No compaction changes, automatic RCA, parallel execution, resume, comparison CLI or generic benchmark platform are introduced here.
