import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from context_manager.store import Store
from context_manager.vault import ProjectVault


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

    def test_quoted_multiline_secret_is_never_persisted(self):
        self.store.sync_requirements('Set password="correct horse battery staple"')
        raw = self.db.read_bytes()
        for word in (b"correct", b"horse", b"battery", b"staple"):
            self.assertNotIn(word, raw)

    def test_manual_acceptance_criteria_are_redacted(self):
        self.store.add_task("Configure authentication", acceptance=['password="another private phrase"'])
        raw = self.db.read_bytes()
        for word in (b"another", b"private", b"phrase"):
            self.assertNotIn(word, raw)

    def test_prefixed_environment_secrets_are_redacted(self):
        self.store.sync_requirements('Add service config. OPENAI_API_KEY="openai private value". DB_PASSWORD=db-private-value')
        raw = self.db.read_bytes()
        for value in (b"openai", b"private", b"value", b"db-private-value"):
            self.assertNotIn(value, raw)

    def test_relevant_file_paths_are_redacted_before_persistence(self):
        task_id, _ = self.store.add_task("Review service configuration")
        secret = "supersecretvalue123456"
        self.store.add_relevant_files(task_id, [f"config/API_KEY={secret}.env"])

        task = self.store.list_tasks()[0]
        self.assertEqual(json.loads(task["relevant_files"]), ["config/API_KEY=[REDACTED]"])
        for database_file in (self.db, Path(f"{self.db}-wal")):
            if database_file.exists():
                self.assertNotIn(secret.encode(), database_file.read_bytes())

    def test_ambiguous_cancel_and_change_preserve_tasks(self):
        self.store.sync_requirements("Add email recovery. Add sms recovery.")
        before = {task["id"]: task["description"] for task in self.store.list_tasks()}
        cancelled = self.store.sync_requirements("Cancel recovery")
        changed = self.store.sync_requirements("Change recovery to passkey recovery")
        self.assertEqual(cancelled["ambiguous"], ["Cancel recovery"])
        self.assertEqual(changed["ambiguous"], ["Change recovery to passkey recovery"])
        after = {task["id"]: task["description"] for task in self.store.list_tasks()}
        self.assertEqual(after, before)

    def test_changed_requirement_merges_existing_destination(self):
        self.store.sync_requirements("Add old email recovery. Add passkey recovery.")
        result = self.store.sync_requirements("Change old email recovery to passkey recovery")
        rows = self.store.list_tasks()
        active = [task for task in rows if task["status"] != "cancelled"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["description"], "Add passkey recovery")
        self.assertEqual(result["updated"], [active[0]["id"]])

    def test_duplicate_task_merges_new_acceptance_criteria(self):
        task_id, created = self.store.add_task("Implement account recovery", acceptance=["Email is sent"])
        duplicate_id, duplicate_created = self.store.add_task("Build account recovery", acceptance=["Token expires", "Email is sent"])
        self.assertEqual(task_id, duplicate_id)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        task = self.store.list_tasks()[0]
        self.assertEqual(json.loads(task["acceptance"]), ["Email is sent", "Token expires"])

    def test_invalid_mcp_json_does_not_reuse_previous_request_id(self):
        from context_manager.mcp_server import main as mcp_main
        output = io.StringIO()
        requests = '{"jsonrpc":"2.0","id":7,"method":"initialize","params":{}}\nnot-json\n'
        with patch("sys.stdin", io.StringIO(requests)), redirect_stdout(output):
            mcp_main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], 7)
        self.assertIsNone(responses[1]["id"])
        self.assertEqual(responses[1]["error"]["code"], -32700)

    def test_mcp_invalid_request_and_unknown_method_get_responses(self):
        from context_manager.mcp_server import main as mcp_main
        output = io.StringIO()
        requests = (
            '[]\n'
            '{"jsonrpc":"2.0","id":8,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n'
            '{"jsonrpc":"2.0","id":9,"method":"unknown/method"}\n'
        )
        with patch("sys.stdin", io.StringIO(requests)), redirect_stdout(output):
            mcp_main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["id"], 8)
        self.assertEqual(responses[2]["id"], 9)
        self.assertEqual(responses[2]["error"]["code"], -32601)

    def test_mcp_tool_errors_are_returned_as_recoverable_tool_results(self):
        from context_manager.mcp_server import main as mcp_main
        output = io.StringIO()
        requests = [
            {"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "missing_tool", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "sync_requirements", "arguments": {}}},
        ]
        with patch.dict(os.environ, {"CWM_PROJECT_ROOT": self.temp.name}), patch(
            "sys.stdin", io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
        ), redirect_stdout(output):
            mcp_main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[1]["id"], 10)
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertEqual(responses[2]["id"], 11)
        self.assertTrue(responses[2]["result"]["isError"])

    def test_mcp_tool_notification_does_not_execute_mutation(self):
        from context_manager.mcp_server import main as mcp_main
        output = io.StringIO()
        request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "sync_requirements", "arguments": {"text": "Must not persist"}}}
        with patch("sys.stdin", io.StringIO(json.dumps(request) + "\n")), patch.dict(os.environ, {"CWM_DB_PATH": str(self.db)}), redirect_stdout(output):
            mcp_main()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.store.list_tasks(), [])

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

    def test_claude_precompact_emits_compaction_instructions_as_text(self):
        from context_manager.cli import main
        self.store.sync_requirements("Keep the project recovery checklist.")
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"trigger":"auto"}')), redirect_stdout(output):
            code = main(["--root", self.temp.name, "hook", "--host", "claude", "--event", "PreCompact"])
        self.assertEqual(code, 0)
        self.assertIn("Persistent project checklist", output.getvalue())
        self.assertIn("Keep the project recovery checklist", output.getvalue())

    def test_codex_precompact_only_persists_checkpoint(self):
        from context_manager.cli import main
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"trigger":"auto"}')), redirect_stdout(output):
            code = main(["--root", self.temp.name, "hook", "--host", "codex", "--event", "PreCompact"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertIsNotNone(self.store.recover()["checkpoint"])

    def test_e2e_new_requirement_context_reset_and_recovery(self):
        self.store.sync_requirements("Implement a task checklist. Add reliable checkpoint recovery.")
        self.store.set_status(self.store.list_tasks()[0]["id"], "completed", "test: checklist persistence passed")
        self.store.checkpoint("Checklist saved", "Persisted first requirement", "Implement recovery")
        self.store.close()
        self.store = Store(self.db)
        recovered = self.store.recover()
        self.assertEqual(recovered["remaining_count"], 1)
        self.assertIn("recovery", recovered["active_tasks"][0]["description"])


class ProjectVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = ProjectVault(Path(self.temp.name) / "vault")

    def tearDown(self):
        self.temp.cleanup()

    def test_aliases_and_properties_are_saved_resolved_and_searchable(self):
        note = self.vault.create_note(
            "Account recovery",
            "Use passkeys when available.",
            ["security"],
            ["Login recovery"],
            {"status": "accepted", "reviewed": True, "priority": 2, "channels": ["email", "passkey"]},
        )
        self.assertEqual(self.vault.read_note("Login recovery")["id"], note["id"])
        self.assertEqual(len(self.vault.search_notes("tag:#security [status:accepted]")), 1)
        self.assertEqual(len(self.vault.search_notes("[reviewed:true] [priority:2]")), 1)
        self.assertEqual(self.vault.search_notes("[status:draft]"), [])
        self.assertIn("aliases:", (self.vault.root / note["path"]).read_text(encoding="utf-8"))

    def test_null_properties_match_scalar_and_list_filters(self):
        self.vault.create_note("Nullable fields", properties={"status": None, "choices": [None, "ready"]})
        self.assertEqual(len(self.vault.search_notes("[status:null]")), 1)
        self.assertEqual(len(self.vault.search_notes("[choices:null]")), 1)

    def test_four_space_frontmatter_lists_support_tags_and_aliases(self):
        self.vault.root.mkdir(parents=True)
        (self.vault.root / "manual.md").write_text(
            '---\ntitle: "Manual note"\ntags:\n    - research\naliases:\n    - Alternate name\nstatus: ready\n---\n\nBody.\n',
            encoding="utf-8",
        )
        self.assertEqual(self.vault.read_note("Alternate name")["title"], "Manual note")
        self.assertEqual(len(self.vault.search_notes("tag:#research [status:ready]")), 1)

    def test_json_frontmatter_and_nested_yaml_are_interpreted_safely(self):
        self.vault.root.mkdir(parents=True)
        (self.vault.root / "json.md").write_text(
            '---\n{"title":"JSON note","tags":["json"],"aliases":["JSON alias"],"source":{"url":"https://example.com"}}\n---\n\nBody.\n',
            encoding="utf-8",
        )
        (self.vault.root / "nested.md").write_text(
            '---\ntitle: "Nested map"\nsource:\n  url: https://example.com\n---\n\nBody.\n',
            encoding="utf-8",
        )
        self.assertEqual(self.vault.read_note("JSON alias")["title"], "JSON note")
        self.assertEqual(len(self.vault.search_notes("tag:#json")), 1)
        self.assertEqual(self.vault.search_notes("[source:null]"), [])
        self.assertEqual(self.vault.search_notes("[source]"), [])

    def test_titles_and_aliases_with_link_syntax_characters_resolve(self):
        note = self.vault.create_note("C# Setup", aliases=["C# Quickstart", "Title | Alias"])
        self.assertEqual(self.vault.read_note("C# Setup")["id"], note["id"])
        self.assertEqual(self.vault.read_note("C# Quickstart")["id"], note["id"])
        self.assertEqual(self.vault.read_note("Title | Alias")["id"], note["id"])
        self.vault.create_note("Link to hash title", "[[C# Setup]]")
        self.assertEqual(self.vault.note_links("Link to hash title")[0]["resolved"]["id"], note["id"])

    def test_markdown_links_resolve_outgoing_and_backlinks(self):
        target = self.vault.create_note("Target note")
        source_path = self.vault.root / "nested" / "Source.md"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            '---\ntitle: "Source note"\n---\n\nSee [the target][home].\n'
            '[home]: ../target-note.md "Target note"\n'
            'The target title is [another reference](../target-note.md (the target)).\n'
            'The draft is [another](chapter (draft).md).\n',
            encoding="utf-8",
        )
        draft_path = self.vault.root / "nested" / "chapter (draft).md"
        draft_path.write_text('---\ntitle: "Draft chapter"\n---\n\nDraft.\n', encoding="utf-8")
        outgoing = self.vault.note_links("Source note")
        self.assertEqual(len(outgoing), 2)
        target_link = next(item for item in outgoing if item["target"] == "../target-note.md")
        draft_link = next(item for item in outgoing if item["target"] == "chapter (draft).md")
        self.assertEqual(target_link["type"], "markdown")
        self.assertEqual(target_link["resolved"]["id"], target["id"])
        self.assertEqual(draft_link["resolved"]["title"], "Draft chapter")
        self.assertEqual(self.vault.backlinks(target["id"])[0]["title"], "Source note")
        self.assertEqual(self.vault.mentions(target["id"]), [])

    def test_mentions_find_only_unlinked_prose_and_aliases(self):
        target = self.vault.create_note("Curated knowledge", aliases=["Human knowledge"])
        self.vault.create_note(
            "References",
            "Curated knowledge appears here. Human knowledge too. [[Curated knowledge]] "
            "[Curated knowledge](curated-knowledge.md) `Human knowledge`\n\n"
            "```md\nCurated knowledge\n```\n",
        )
        mentions = self.vault.mentions(target["id"])
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["count"], 2)

    def test_create_rejects_nested_properties(self):
        with self.assertRaises(ValueError):
            self.vault.create_note("Nested", properties={"source": {"url": "https://example.com"}})
        with self.assertRaises(ValueError):
            self.vault.create_note("Nested list", properties={"items": [["nested"]]})

    def test_mcp_exposes_curated_note_operations(self):
        from context_manager.mcp_server import TOOLS, main as mcp_main
        create_schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == "create_note")
        property_schema = create_schema["properties"]["properties"]
        array_schema = next(item for item in property_schema["additionalProperties"]["oneOf"] if item["type"] == "array")
        self.assertNotIn("array", array_schema["items"]["type"])
        self.assertEqual(property_schema["maxProperties"], 16)
        self.assertIn("[iI][dD]", property_schema["propertyNames"]["pattern"])
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "create_note", "arguments": {
                "title": "Curated note", "aliases": ["Human note"], "tags": ["knowledge"],
                "properties": {"status": "reviewed"}, "content": "A durable fact."}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_notes", "arguments": {
                "query": "tag:#knowledge [status:reviewed]"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "note_mentions", "arguments": {
                "reference": "Human note"}}},
        ]
        output = io.StringIO()
        with patch.dict(os.environ, {"CWM_PROJECT_ROOT": self.temp.name}), patch(
            "sys.stdin", io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        ), redirect_stdout(output):
            mcp_main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        available_tools = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("note_mentions", available_tools)
        matches = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(matches[0]["title"], "Curated note")
        self.assertEqual(json.loads(responses[4]["result"]["content"][0]["text"]), [])


if __name__ == "__main__":
    unittest.main()
