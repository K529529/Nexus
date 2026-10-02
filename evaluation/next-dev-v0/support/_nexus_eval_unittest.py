"""Capture unittest outcomes without changing test selection or assertions."""

import json
import os
import runpy
import sys
import unittest

_results = {}


class RecordingResult(unittest.TextTestResult):
    def record(self, test, status):
        _results[str(test)] = status
        description = test.shortDescription()
        if description:
            _results[description] = status

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "failed")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped")


unittest.TextTestResult = RecordingResult
unittest.TextTestRunner.resultclass = RecordingResult
sys.argv.pop(0)
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))
exit_code = 2
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
    exit_code = 0
except SystemExit as exc:
    exit_code = int(exc.code or 0)
finally:
    with open(os.environ["NEXUS_EVAL_TEST_RESULTS"], "w", encoding="utf-8") as stream:
        json.dump({"exit_code": exit_code, "tests": _results}, stream, sort_keys=True)
sys.exit(exit_code)
