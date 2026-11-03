"""Exercise the actual R2 maintenance guard across events and prerequisite failures."""
import ast
from pathlib import Path
import re
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cache-archives.yml"
PREREQUISITES = ("archive_source", "archive_disk", "archive_restore", "archive_tools")


def eligible(event, status, **outcomes):
    source = WORKFLOW.read_text()
    block = source.split("- name: Refresh the R2 bucket (optional)\n", 1)[1]
    expression = re.search(r"if: \$\{\{ (.+) \}\}", block).group(1)
    expression = expression.replace("success()", repr(status == "success"))
    expression = expression.replace("failure()", repr(status == "failure"))
    expression = expression.replace("github.event_name", repr(event))
    for step in PREREQUISITES:
        expression = expression.replace(f"steps.{step}.outcome", repr(outcomes.get(step, "success")))
    tree = ast.parse(expression.replace("&&", "and").replace("||", "or"), mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.Constant)
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise AssertionError("Unexpected workflow expression syntax")
    return eval(compile(tree, "<R2 maintenance guard>", "eval"), {"__builtins__": {}})


class MaintenanceSchedule(unittest.TestCase):
    def test_push_pr_and_release_do_not_wait_for_bucket_maintenance(self):
        for event in ("push", "pull_request", "release"):
            for status in ("success", "failure", "cancelled"):
                with self.subTest(event=event, status=status):
                    self.assertFalse(eligible(event, status))

    def test_schedule_and_dispatch_preserve_status_and_real_prerequisites(self):
        for event in ("schedule", "workflow_dispatch"):
            for status in ("success", "failure"):
                with self.subTest(event=event, status=status):
                    self.assertTrue(eligible(event, status))
                    for step in PREREQUISITES:
                        for outcome in ("failure", "skipped", "cancelled", ""):
                            self.assertFalse(eligible(event, status, **{step: outcome}))
            self.assertFalse(eligible(event, "cancelled"))


if __name__ == "__main__":
    unittest.main()
