import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from context_manager.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / ".context-manager" / "state.sqlite3"
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_checklist_from_request(self):
        result = self.store.sync_requirements("Build the login page. Add password reset.")
        self.assertEqual(len(result["added"]), 2)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_new_requirements_append_without_replacing(self):
        self.store.sync_requirements("Build the login page.")
        result = self.store.sync_requirements("Also add Google login.")
        descriptions = [t["description"] for t in self.store.list_tasks()]
        self.assertIn("Build the login page", descriptions)
        self.assertTrue(any("google login" in description.casefold() for description in descriptions))
        self.assertEqual(len(result["added"]), 1)

    def test_duplicate_task_not_added(self):
        self.store.sync_requirements("Add user login.")
        result = self.store.sync_requirements("Add user login.")
        self.assertEqual(len(result["added"]), 0)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_changed_and_cancelled_requirements_are_audited(self):
        self.store.sync_requirements("Add password reset.")
        result = self.store.sync_requirements("Change password reset to email-only recovery.")
        self.assertEqual(len(result["updated"]), 1)
        self.assertEqual(self.store.list_tasks()[0]["description"], "email-only recovery")
        cancelled = self.store.sync_requirements("We no longer need email-only recovery")
        self.assertEqual(len(cancelled["cancelled"]), 1)

    def test_unfinished_tasks_recover_after_restart(self):
        self.store.sync_requirements("Implement session recovery.")
        self.store.checkpoint("Recovery core started", "Added storage", "Add CLI recovery command")
        self.store.close()
        self.store = Store(self.db)
        bundle = self.store.recover()
        self.assertEqual(bundle["remaining_count"], 1)
        self.assertEqual(bundle["checkpoint"]["next_action"], "Add CLI recovery command")

    def test_dependency_graph_and_cycle_guard(self):
        a, _ = self.store.add_task("Implement storage")
        b, _ = self.store.add_task("Add recovery interface")
        self.store.set_dependencies(b, [a])
        with self.assertRaises(ValueError):
            self.store.set_dependencies(a, [b])
        row = next(t for t in self.store.list_tasks() if t["id"] == b)
        self.assertEqual(json.loads(row["dependencies"]), [a])
        with self.assertRaises(ValueError):
            self.store.set_status(b, "completed", "test: dependent task passed")
        self.store.set_status(a, "completed", "test: prerequisite task passed")
        self.store.set_status(b, "completed", "test: dependent task passed")

    def test_completion_requires_structured_verification(self):
        task, _ = self.store.add_task("Verify persistence")
        with self.assertRaises(ValueError):
            self.store.set_status(task, "completed", "looks good")
        self.store.set_status(task, "completed", "test: unittest suite passed")
        self.assertEqual(self.store.list_tasks()[0]["status"], "completed")

    def test_atomic_rollback_on_interrupted_sync(self):
        original = self.store.add_task
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated process interruption")
            return original(*args, **kwargs)
        with patch.object(self.store, "add_task", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.store.sync_requirements("First task. Second task.")
        self.assertEqual(self.store.list_tasks(), [])

    def test_project_isolation(self):
        self.store.sync_requirements("Project alpha requirement.")
        other = Store(Path(self.temp.name) / "other" / ".context-manager" / "state.sqlite3")
        self.assertEqual(other.list_tasks(), [])
        other.close()

    def test_secret_values_are_never_persisted(self):
        self.store.sync_requirements("Build the login page. API_KEY=supersecretvalue123456")
        raw = self.db.read_bytes()
        self.assertNotIn(b"supersecretvalue123456", raw)
        self.assertTrue(self.store.list_tasks())

    def test_capture_can_be_disabled(self):
        with patch.dict(os.environ, {"CWM_DISABLE_CAPTURE": "1"}):
            result = self.store.sync_requirements("Never store this requirement")
        self.assertIn("disabled", result)
        self.assertEqual(self.store.list_tasks(), [])

    def test_context_budget_and_host_tool_fallback(self):
        self.store.sync_requirements("Implement a compact context recovery system.")
        bundle = self.store.context_bundle("context recovery", 500)
        self.assertLessEqual(bundle["character_budget"], 500)
        self.assertLessEqual(len(json.dumps(bundle, ensure_ascii=False)), 500)
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.store.checkpoint("x" * 400, "y" * 900, "z" * 900)
        bounded = self.store.context_bundle("", 256)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False)), 256)

    def test_host_hook_falls_back_without_optional_host_services(self):
        from context_manager.cli import main
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"prompt":"Add a recovery command"}')), redirect_stdout(output):
            code = main(["--root", self.temp.name, "hook", "--host", "freebuff", "--event", "UserPromptSubmit"])
        self.assertEqual(code, 0)
        self.assertIn("context", json.loads(output.getvalue()))
        probe = Store(self.db)
        self.assertTrue(probe.list_tasks())
        probe.close()

    def test_cursor_precompact_uses_observational_output_schema(self):
        from context_manager.cli import main
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"trigger":"auto"}')), redirect_stdout(output):
            code = main(["--root", self.temp.name, "hook", "--host", "cursor", "--event", "preCompact"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"user_message"})

    def test_e2e_new_requirement_context_reset_and_recovery(self):
        self.store.sync_requirements("Implement a task checklist. Add reliable checkpoint recovery.")
        self.store.set_status(self.store.list_tasks()[0]["id"], "completed", "test: checklist persistence passed")
        self.store.checkpoint("Checklist saved", "Persisted first requirement", "Implement recovery")
        self.store.close()
        self.store = Store(self.db)
        recovered = self.store.recover()
        self.assertEqual(recovered["remaining_count"], 1)
        self.assertIn("recovery", recovered["active_tasks"][0]["description"])


if __name__ == "__main__":
    unittest.main()
