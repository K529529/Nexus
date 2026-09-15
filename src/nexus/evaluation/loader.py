"""Declarative TOML loader and structural validation for Day 9 cases."""

from __future__ import annotations

import tomllib
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from nexus.domain.tooling import RiskLevel
from nexus.evaluation.harness import (
    EvalHarnessCommandPolicy,
    _is_absolute_executable,
    _resolve_eval_executables,
)
from nexus.evaluation.models import (
    EvalAssertion,
    EvalAssertionType,
    EvalCase,
    EvalCaseLoadError,
    EvalHarnessOperation,
    EvalSuiteLoadResult,
    RequiredFact,
)

MANDATORY_CASE_IDS = tuple(f"EVAL-{number:03d}" for number in range(1, 7))
_SECURITY_CODES = {
    "PLAN_SCOPE_DENIED",
    "PERMISSION_DENIED",
    "COMMAND_DENIED",
    "MCP_WRITE_NOT_AUTHORIZED",
}


class EvalSuiteLoader:
    def load_case(self, case_path: Path) -> EvalCase:
        source = case_path / "case.toml" if case_path.is_dir() else case_path
        data = tomllib.loads(source.read_text(encoding="utf-8"))
        assertions_raw = _list(data, "deterministic_success_conditions")
        assertions = tuple(_assertion(item) for item in assertions_raw)
        case = EvalCase(
            case_id=_string(data, "case_id"),
            initial_repo_state=_string(data, "initial_repo_state"),
            task=_string(data, "task"),
            allowed_files=_strings(data, "allowed_files"),
            forbidden_files=_strings(data, "forbidden_files"),
            pre_validation_command=_strings(data, "pre_validation_command"),
            validation_command=_strings(data, "validation_command"),
            validation_timeout_seconds=_optional_int(data, "validation_timeout_seconds"),
            test_discovery_command=_strings(data, "test_discovery_command"),
            expected_behavior=_string(data, "expected_behavior"),
            deterministic_success_conditions=assertions,
            max_reasonable_steps=_integer(data, "max_reasonable_steps"),
            metadata=_metadata(data.get("metadata")),
        )
        _validate_case(case, source.parent)
        return case

    def load_suite(self, cases_root: Path) -> EvalSuiteLoadResult:
        cases: list[EvalCase] = []
        errors: list[EvalCaseLoadError] = []
        seen: set[str] = set()
        for source in sorted(cases_root.glob("*/case.toml")):
            hint = source.parent.name or None
            try:
                case = self.load_case(source)
                if case.case_id in seen:
                    raise ValueError(f"Duplicate case_id: {case.case_id}")
                seen.add(case.case_id)
                cases.append(case)
            except Exception as exc:
                errors.append(
                    EvalCaseLoadError(
                        source_path=str(source),
                        case_id_hint=hint,
                        code="EVAL_CASE_INVALID",
                        message=str(exc),
                    )
                )
        for missing in sorted(set(MANDATORY_CASE_IDS) - seen):
            errors.append(
                EvalCaseLoadError(
                    source_path=str(cases_root),
                    case_id_hint=missing,
                    code="MANDATORY_CASE_MISSING",
                    message=f"Mandatory evaluation case {missing} is missing.",
                )
            )
        return EvalSuiteLoadResult(
            cases=tuple(sorted(cases, key=lambda item: item.case_id)),
            errors=tuple(errors),
        )


def _assertion(data: object) -> EvalAssertion:
    if not isinstance(data, dict):
        raise ValueError("Each deterministic success condition must be a table.")
    try:
        assertion_type = EvalAssertionType(_string(data, "assertion_type"))
    except ValueError as exc:
        raise ValueError("Unknown evaluation assertion type.") from exc
    facts = tuple(_fact(item) for item in _optional_list(data, "required_facts"))
    assertion = EvalAssertion(
        assertion_type=assertion_type,
        path=_optional_string(data, "path"),
        expected_text=_optional_string(data, "expected_text"),
        required_facts=facts,
        security_codes=_optional_strings(data, "security_codes"),
        expected_test_node_ids=_optional_strings(data, "expected_test_node_ids"),
        metadata=_metadata(data.get("metadata")),
    )
    _validate_assertion(assertion)
    return assertion


def _fact(data: object) -> RequiredFact:
    if not isinstance(data, dict):
        raise ValueError("Required facts must be tables.")
    return RequiredFact(
        fact_id=unicodedata.normalize("NFC", _string(data, "fact_id")),
        description=unicodedata.normalize("NFC", _string(data, "description")),
        required_terms=tuple(
            unicodedata.normalize("NFC", item) for item in _strings(data, "required_terms")
        ),
    )


def _validate_case(case: EvalCase, case_directory: Path) -> None:
    if not case.case_id or not case.task.strip() or not case.expected_behavior.strip():
        raise ValueError("Case identifiers, task, and expected behavior must be non-empty.")
    if case.max_reasonable_steps < 1:
        raise ValueError("max_reasonable_steps must be positive.")
    timeout = case.validation_timeout_seconds
    if timeout is not None and not 1 <= timeout <= 300:
        raise ValueError("validation_timeout_seconds must be in 1..300.")
    _repository_path(case.initial_repo_state, allow_dot=True)
    fixture = case_directory / PurePosixPath(case.initial_repo_state)
    if not fixture.exists() or not fixture.is_dir():
        raise ValueError("initial_repo_state must name an existing fixture directory.")
    try:
        fixture.resolve(strict=True).relative_to(case_directory.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("initial_repo_state escapes the case directory.") from exc
    for path in (*case.allowed_files, *case.forbidden_files):
        _repository_path(path)
    if len(set(case.allowed_files)) != len(case.allowed_files):
        raise ValueError("allowed_files must be unique.")
    if len(set(case.forbidden_files)) != len(case.forbidden_files):
        raise ValueError("forbidden_files must be unique.")
    if set(case.allowed_files) & set(case.forbidden_files):
        raise ValueError("A path cannot be both allowed and forbidden.")
    if not case.deterministic_success_conditions:
        raise ValueError("At least one deterministic assertion is required.")
    fact_ids = [
        fact.fact_id
        for assertion in case.deterministic_success_conditions
        for fact in assertion.required_facts
    ]
    if len(fact_ids) != len(set(fact_ids)):
        raise ValueError("Required fact ids must be unique within a case.")
    _validate_commands(case, case_directory)
    _validate_mandatory_case(case)


def _validate_assertion(assertion: EvalAssertion) -> None:
    required = {
        EvalAssertionType.REQUIRED_PATH_CHANGED: {"path"},
        EvalAssertionType.REQUIRED_PATH_CREATED: {"path"},
        EvalAssertionType.REQUIRED_TEXT_IN_FILE: {"path", "expected_text"},
        EvalAssertionType.REQUIRED_FINAL_FACTS: {"required_facts"},
        EvalAssertionType.REQUIRED_SECURITY_EVIDENCE: {"security_codes"},
        EvalAssertionType.TARGETED_TEST_ADDED: {"path", "expected_test_node_ids"},
    }.get(assertion.assertion_type, set())
    present = set()
    if assertion.path is not None:
        present.add("path")
        _repository_path(assertion.path)
    if assertion.expected_text is not None:
        present.add("expected_text")
    if assertion.required_facts:
        present.add("required_facts")
    if assertion.security_codes:
        present.add("security_codes")
    if assertion.expected_test_node_ids:
        present.add("expected_test_node_ids")
    if present != required:
        raise ValueError(f"Assertion {assertion.assertion_type.value} has incompatible parameters.")
    for fact in assertion.required_facts:
        if not fact.fact_id or not fact.description.strip() or not fact.required_terms:
            raise ValueError("Required facts and their terms must be non-empty.")
        if any(not term for term in fact.required_terms):
            raise ValueError("Required fact terms must be non-empty.")
    if any(code not in _SECURITY_CODES for code in assertion.security_codes):
        raise ValueError("Security evidence uses an unapproved error code.")
    if len(set(assertion.expected_test_node_ids)) != len(assertion.expected_test_node_ids):
        raise ValueError("Expected pytest node ids must be unique.")
    for node in assertion.expected_test_node_ids:
        if "::" not in node:
            raise ValueError("Expected pytest node ids must include a node suffix.")
        _repository_path(node.split("::", 1)[0])


def _validate_commands(case: EvalCase, case_directory: Path) -> None:
    for command, discovery in (
        (case.pre_validation_command, False),
        (case.validation_command, False),
        (case.test_discovery_command, True),
    ):
        if not command:
            continue
        operation = (
            EvalHarnessOperation.TEST_DISCOVERY if discovery else EvalHarnessOperation.VALIDATION
        )
        if _is_absolute_executable(command[0]):
            try:
                policy = EvalHarnessCommandPolicy(_resolve_eval_executables(case_directory))
                valid = (
                    policy.classify(
                        operation=operation.value,
                        arguments={"argv": command},
                    )
                    is RiskLevel.SAFE
                )
            except Exception:
                valid = False
            if not valid:
                raise ValueError(f"Command is outside the Day 9 Harness grammar: {command!r}")
            continue
        if "/" in command[0] or "\\" in command[0]:
            raise ValueError(f"Command is outside the Day 9 Harness grammar: {command!r}")
        executable = Path(command[0]).name.casefold().removesuffix(".exe")
        if discovery:
            valid = executable == "pytest" and EvalHarnessCommandPolicy._valid_discovery(
                command[1:]
            )
        else:
            valid = EvalHarnessCommandPolicy._valid_validation(executable, command[1:])
        if not valid:
            raise ValueError(f"Command is outside the Day 9 Harness grammar: {command!r}")


def _validate_mandatory_case(case: EvalCase) -> None:
    assertions = case.deterministic_success_conditions
    types = [item.assertion_type for item in assertions]
    if case.case_id in {"EVAL-001", "EVAL-002", "EVAL-003"}:
        if (
            not case.pre_validation_command
            or case.pre_validation_command != case.validation_command
        ):
            raise ValueError(
                f"{case.case_id} requires equal non-empty pre/post validation commands."
            )
        if case.test_discovery_command:
            raise ValueError(f"{case.case_id} test discovery command must be empty.")
        if case.case_id in {"EVAL-001", "EVAL-002"} and (
            Path(case.pre_validation_command[0]).name.casefold().removesuffix(".exe") != "pytest"
        ):
            raise ValueError(f"{case.case_id} requires pytest pre/post validation.")
        common = {
            EvalAssertionType.VALIDATION_PASSES,
            EvalAssertionType.NO_UNAUTHORIZED_CHANGES,
            EvalAssertionType.NO_FORBIDDEN_CHANGES,
        }
        if not common.issubset(types):
            raise ValueError(f"{case.case_id} is missing mandatory postconditions.")
        required_type = (
            EvalAssertionType.REQUIRED_PATH_CHANGED if case.case_id != "EVAL-002" else None
        )
        if required_type and required_type not in types:
            raise ValueError(f"{case.case_id} requires a changed-path assertion.")
        if case.case_id == "EVAL-002" and not any(
            item in types
            for item in (
                EvalAssertionType.REQUIRED_PATH_CHANGED,
                EvalAssertionType.REQUIRED_PATH_CREATED,
            )
        ):
            raise ValueError("EVAL-002 requires a path change/create assertion.")
    elif case.case_id == "EVAL-004":
        targeted = [
            item
            for item in assertions
            if item.assertion_type is EvalAssertionType.TARGETED_TEST_ADDED
        ]
        if len(targeted) != 1:
            raise ValueError("EVAL-004 requires exactly one TARGETED_TEST_ADDED assertion.")
        common = {
            EvalAssertionType.VALIDATION_PASSES,
            EvalAssertionType.NO_UNAUTHORIZED_CHANGES,
            EvalAssertionType.NO_FORBIDDEN_CHANGES,
        }
        if not common.issubset(types):
            raise ValueError("EVAL-004 is missing mandatory postconditions.")
        assertion = targeted[0]
        assert assertion.path is not None
        if (
            not case.pre_validation_command
            or not case.validation_command
            or not case.test_discovery_command
            or case.pre_validation_command == case.validation_command
        ):
            raise ValueError("EVAL-004 requires distinct explicit pre/post/discovery commands.")
        post_targets = _pytest_targets(case.validation_command)
        pre_targets = set(_pytest_targets(case.pre_validation_command))
        expected = set(assertion.expected_test_node_ids)
        if "-vv" not in case.validation_command or "-q" in case.validation_command:
            raise ValueError("EVAL-004 post-validation requires -vv and forbids -q.")
        if not expected.issubset(post_targets) or expected & pre_targets:
            raise ValueError("EVAL-004 expected nodes must be post-targeted and not pre-targeted.")
        if any(node.split("::", 1)[0] != assertion.path for node in expected):
            raise ValueError("EVAL-004 node ids must belong to TARGETED_TEST_ADDED.path.")
        if assertion.path not in case.test_discovery_command[3:]:
            raise ValueError("EVAL-004 discovery must target the asserted test file.")
    elif case.case_id == "EVAL-005":
        if case.pre_validation_command or case.validation_command or case.test_discovery_command:
            raise ValueError("EVAL-005 commands must all be empty.")
        if (
            EvalAssertionType.NO_REPOSITORY_CHANGES not in types
            or EvalAssertionType.REQUIRED_FINAL_FACTS not in types
        ):
            raise ValueError("EVAL-005 requires no-change and final-facts assertions.")
    elif case.case_id == "EVAL-006":
        if case.pre_validation_command or case.validation_command or case.test_discovery_command:
            raise ValueError("Mandatory EVAL-006 commands must all be empty.")
        required = {
            EvalAssertionType.NO_UNAUTHORIZED_CHANGES,
            EvalAssertionType.NO_FORBIDDEN_CHANGES,
            EvalAssertionType.REQUIRED_SECURITY_EVIDENCE,
        }
        if not required.issubset(types):
            raise ValueError("EVAL-006 requires repository and formal security assertions.")


def _pytest_targets(command: tuple[str, ...]) -> set[str]:
    return {item for item in command[1:] if not item.startswith("-") and not item.isdigit()}


def _repository_path(value: str, *, allow_dot: bool = False) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or ".." in path.parts
        or "\\" in value
        or (value == "." and not allow_dot)
        or value != path.as_posix()
    ):
        raise ValueError(f"Invalid repository-relative POSIX path: {value!r}")


def _string(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _optional_string(data: dict[str, object], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    return value


def _strings(data: dict[str, object], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings.")
    return tuple(value)


def _optional_strings(data: dict[str, object], name: str) -> tuple[str, ...]:
    return () if name not in data else _strings(data, name)


def _integer(data: dict[str, object], name: str) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    return value


def _optional_int(data: dict[str, object], name: str) -> int | None:
    return None if data.get(name) is None else _integer(data, name)


def _list(data: dict[str, object], name: str) -> list[object]:
    value = data.get(name)
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of tables.")
    return value


def _optional_list(data: dict[str, object], name: str) -> list[object]:
    return [] if name not in data else _list(data, name)


def _metadata(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not _is_json_value(value):
        raise ValueError("metadata must be a JSON object.")
    return dict(value)


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False
