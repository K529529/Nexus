"""Trusted pytest result capture; loaded only in a fresh validation container."""

import json
import os

_results = {}


def pytest_runtest_logreport(report):
    if report.when == "call" or report.failed or report.skipped:
        status = report.outcome
        if report.failed and report.when != "call":
            status = "error"
        if _results.get(report.nodeid) != "error":
            _results[report.nodeid] = status


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["NEXUS_EVAL_TEST_RESULTS"], "w", encoding="utf-8") as stream:
        json.dump({"exit_code": int(exitstatus), "tests": _results}, stream, sort_keys=True)
