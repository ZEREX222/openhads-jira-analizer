"""Offline tests: synthetic data only, never the operator's Jira or queue."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "jira-task-processor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from _common import Store, TaskError, atomic_json, digest, read_json, redact
from claim_next_task import main as claim_main
from fetch_jira_tasks import SafeRedirect


def raw_issue(key="DEMO-1", updated="2026-01-01T00:00:00.000+0000"):
    return {
        "key": key,
        "fields": {
            "summary": "Synthetic attachment test", "description": {"type": "doc", "content": []},
            "status": {"id": "1", "name": "NEW", "statusCategory": {"key": "new"}},
            "priority": {"name": "High"}, "issuetype": {"name": "Bug"},
            "assignee": {"accountId": "fake-1", "displayName": "Synthetic"},
            "reporter": None, "labels": ["offline"], "components": [{"id": "1", "name": "Demo"}],
            "created": "2026-01-01T00:00:00.000+0000", "updated": updated,
            "attachment": [],
        },
    }


def snapshot(key="DEMO-1", updated="2026-01-01T00:00:00+00:00", content="one"):
    task = {"key": key, "summary": "Synthetic issue", "updated": updated,
            "content_hash": digest(content), "comments": [], "attachments": [],
            "status": {"name": "NEW"}}
    task["version_id"] = digest({"updated": updated, "content_hash": task["content_hash"]})
    return task


@contextmanager
def mock_jira(api_version="3"):
    state = {"issues": [raw_issue()], "comments": [{"id": "1", "body": "first"},
                                                {"id": "2", "body": "second"}],
             "attachment": b"synthetic attachment", "requests": [], "attachment_error": False,
             "declared_size": None, "changed_during_fetch": False,
             "writes": [], "label_error": None, "label_error_after_apply": False,
             "update_counter": 0, "edit_during_label_write": False, "search_error_at": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path)
            parameters = parse_qs(path.query)
            state["requests"].append((path.path, parameters, self.headers.get("Authorization")))
            if path.path == f"/rest/api/{api_version}/myself":
                response = {"accountId": "synthetic-only"}
            elif path.path in ("/rest/api/3/search/jql", "/rest/api/2/search"):
                index = int(parameters.get("nextPageToken", parameters.get("startAt", ["0"]))[0])
                if index == state["search_error_at"]:
                    self.send_error(503)
                    return
                page = deepcopy(state["issues"][index:index + 1])
                for issue in page:
                    issue["fields"]["attachment"] = [{
                        "id": "10", "filename": "../unsafe log.txt", "mimeType": "text/plain",
                        "content": state["base"] + "/attachment/10?signature=fake-signed-secret",
                        "size": state["declared_size"] if state["declared_size"] is not None
                        else len(state["attachment"]), "created": "2026-01-01",
                    }]
                response = {"issues": page}
                if path.path == "/rest/api/3/search/jql":
                    response["isLast"] = index + 1 >= len(state["issues"])
                    if not response["isLast"]:
                        response["nextPageToken"] = str(index + 1)
                else:
                    response["total"] = len(state["issues"])
            elif path.path.endswith("/comment"):
                start = int(parameters.get("startAt", ["0"])[0])
                response = {"comments": state["comments"][start:start + 1],
                            "startAt": start, "total": len(state["comments"])}
            elif "/issue/" in path.path:
                key = path.path.rsplit("/", 1)[1]
                issue = next(item for item in state["issues"] if item["key"] == key)
                response = deepcopy(issue)
                if state["changed_during_fetch"]:
                    response["fields"]["updated"] = "changed"
            elif path.path.startswith("/attachment/"):
                if state["attachment_error"]:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(state["attachment"])
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def do_PUT(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["writes"].append(payload)
            if state["label_error"]:
                self.send_error(state["label_error"])
                return
            key = urlsplit(self.path).path.rsplit("/", 1)[1]
            issue = next(item for item in state["issues"] if item["key"] == key)
            labels = issue["fields"]["labels"]
            for operation in payload["update"]["labels"]:
                if "remove" in operation and operation["remove"] in labels:
                    labels.remove(operation["remove"])
                if "add" in operation and operation["add"] not in labels:
                    labels.append(operation["add"])
            state["update_counter"] += 1
            issue["fields"]["updated"] = f'2026-01-01T00:00:{state["update_counter"]:02d}.000+0000'
            if state["edit_during_label_write"]:
                issue["fields"]["summary"] = "Concurrent user edit"
            if state["label_error_after_apply"]:
                self.send_error(503)
                return
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jira-skill-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "runtime"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("JIRA_")}
        self.env.update({"JIRA_USER": "fake-account", "JIRA_TOKEN": "fake-token-not-real",
                         "JIRA_URL": "https://example.invalid", "PYTHONDONTWRITEBYTECODE": "1"})

    def command(self, script, *args, expected=0, data=None, labels=False):
        if script in ("claim_next_task.py", "complete_task.py", "fail_task.py") and not labels:
            args = (*args, "--no-jira-labels")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script), "--data-dir", str(data or self.data), *args],
            capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def enqueue(self, task):
        with Store(self.data).locked() as store:
            return store.enqueue(task)

    def claim(self):
        return self.command("claim_next_task.py")

    def owned(self, script, claim, *args, expected=0, labels=False):
        return self.command(script, "--issue-key", claim["issue_key"],
                            "--claim-token", claim["claim_token"], *args,
                            expected=expected, labels=labels)

    def write_report(self, claim):
        report = self.data / "results" / claim["issue_key"] / "analysis.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# Synthetic analysis\n\nSource evidence for this test only.\n", encoding="utf-8")
        # Deliberately newer than the claim even on coarse-resolution filesystems.
        timestamp = Path(claim["path"]).stat().st_mtime + 1
        os.utime(report, (timestamp, timestamp))
        return report

    def test_empty_and_single_owner_fifo(self):
        self.assertEqual(self.claim()["status"], "empty")
        self.enqueue(snapshot("DEMO-2"))
        self.enqueue(snapshot("DEMO-1"))
        claim = self.claim()
        self.assertEqual(claim["issue_key"], "DEMO-2")
        again = self.claim()
        self.assertEqual(again["status"], "already_processing")
        self.assertEqual(again["path"], claim["path"])
        self.assertNotIn("claim_token", again)
        wrong = self.command("fail_task.py", "--issue-key", "DEMO-2",
                             "--claim-token", "wrong", "--error", "offline failure", expected=1)
        self.assertEqual(wrong["status"], "error")
        self.assertEqual(len(list((self.data / "processing").glob("*.json"))), 1)

    def test_concurrent_claims_have_one_winner(self):
        self.enqueue(snapshot("DEMO-1"))
        self.enqueue(snapshot("DEMO-2"))

        def call(_):
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "claim_next_task.py"), "--data-dir", str(self.data),
                 "--no-jira-labels"],
                capture_output=True, text=True, env=self.env, timeout=20)
            self.assertIn(result.returncode, (0, 1))
            return json.loads(result.stdout)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(8)))
        self.assertEqual(sum(result.get("claimed", False) for result in results), 1)
        self.assertEqual(len(list((self.data / "processing").glob("*.json"))), 1)
        self.assertEqual(len(list((self.data / "inbox").glob("*.json"))), 1)

    def test_completion_guards_and_changed_completed_issue(self):
        original = snapshot()
        self.assertEqual(self.enqueue(original), "new")
        claim = self.claim()
        self.owned("complete_task.py", claim, expected=1)
        report = self.write_report(claim)
        os.utime(report, (1, 1))
        self.owned("complete_task.py", claim, expected=1)
        self.write_report(claim)
        self.assertEqual(self.owned("complete_task.py", claim)["status"], "completed")
        self.assertEqual(self.enqueue(snapshot()), "unchanged")
        self.assertEqual(self.claim()["status"], "empty")
        self.assertEqual(self.enqueue(snapshot(content="changed without timestamp")), "updated")
        self.assertEqual(self.claim()["status"], "claimed")
        state = read_json(self.data / "state.json")["issues"]["DEMO-1"]["last_completed"]
        self.assertEqual(state["content_hash"], original["content_hash"])
        self.assertTrue(Path(state["result_path"]).exists())

    def test_update_while_processing_and_pending_coalescing(self):
        self.enqueue(snapshot())
        claim = self.claim()
        self.enqueue(snapshot(content="two"))
        queued_at = read_json(self.data / "inbox" / "DEMO-1.json")["queued_at"]
        self.enqueue(snapshot(content="three"))
        self.assertEqual(read_json(Path(claim["path"]))["content_hash"], digest("one"))
        pending = read_json(self.data / "inbox" / "DEMO-1.json")
        self.assertEqual(pending["content_hash"], digest("three"))
        self.assertEqual(pending["queued_at"], queued_at)
        self.write_report(claim)
        self.owned("complete_task.py", claim)
        next_claim = self.claim()
        self.assertEqual(read_json(Path(next_claim["path"]))["content_hash"], digest("three"))

    def test_failure_redaction_and_explicit_retry(self):
        self.enqueue(snapshot())
        claim = self.claim()
        result = self.owned("fail_task.py", claim, "--error",
                            "fake-token-not-real fake-account https://example.invalid Bearer other-secret")
        stored = Path(result["path"]).read_text(encoding="utf-8")
        for secret in ("fake-token-not-real", "fake-account", "https://example.invalid", "other-secret"):
            self.assertNotIn(secret, stored)
        self.assertEqual(self.enqueue(snapshot()), "unchanged")
        self.assertEqual(self.claim()["status"], "empty")
        self.assertEqual(self.command("fail_task.py", "--retry", "DEMO-1")["status"], "retried")
        retry = self.claim()
        self.assertNotEqual(retry["claim_token"], claim["claim_token"])
        self.assertEqual(len(read_json(Path(retry["path"]))["retry_history"]), 1)
        self.owned("fail_task.py", claim, "--error", "Old owner", expected=1)

    def test_stale_recovery_requires_confirmation_and_fences_old_owner(self):
        self.enqueue(snapshot())
        claim = self.claim()
        path = Path(claim["path"])
        task = read_json(path)
        task["claim"]["claimed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        atomic_json(path, task)
        self.assertEqual(self.command("claim_next_task.py", expected=1)["status"], "stale_lock")
        self.command("claim_next_task.py", "--recover-stale", "DEMO-1", expected=1)
        recovery = self.command("claim_next_task.py", "--recover-stale", "DEMO-1",
                                "--confirm-owner-stopped")
        self.assertEqual(recovery["status"], "recovered")
        new_claim = self.claim()
        self.assertNotEqual(new_claim["claim_token"], claim["claim_token"])
        self.owned("fail_task.py", claim, "--error", "Old owner", expected=1)

    def test_stale_recovery_preserves_newer_pending(self):
        self.enqueue(snapshot())
        claim = self.claim()
        path = Path(claim["path"])
        task = read_json(path)
        task["claim"]["claimed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        atomic_json(path, task)
        self.enqueue(snapshot(content="newer"))
        self.command("claim_next_task.py", "--recover-stale", "DEMO-1", "--confirm-owner-stopped")
        self.assertEqual(read_json(self.data / "inbox" / "DEMO-1.json")["content_hash"], digest("newer"))
        self.assertEqual(len(list((self.data / "failed").glob("*.json"))), 1)

    def test_multiple_processing_files_are_reported(self):
        self.enqueue(snapshot())
        claim = self.claim()
        atomic_json(self.data / "processing" / "DEMO-2.json", read_json(Path(claim["path"])))
        self.assertIn("Multiple processing", self.command("claim_next_task.py", expected=1)["errors"][0])

    def test_interrupted_move_recovers_state_before_and_after_rename(self):
        for renamed in (False, True):
            data = Path(self.temporary.name) / f"journal-{renamed}"
            with Store(data).locked() as store:
                task = snapshot()
                store.enqueue(task)
                source = data / "inbox" / "DEMO-1.json"
                destination = data / "processing" / "DEMO-1.json"
                task["claim"] = {"token": "test-owner", "claimed_at": datetime.now(timezone.utc).isoformat()}
                store.state["test_recovered"] = True
                atomic_json(data / ".transition.json", {
                    "source": str(source.relative_to(data)), "destination": str(destination.relative_to(data)),
                    "state": store.state, "task": task,
                })
                if renamed:
                    atomic_json(source, task)
                    os.replace(source, destination)
            result = self.command("claim_next_task.py", data=data)
            self.assertEqual(result["status"], "already_processing")
            self.assertTrue(read_json(data / "state.json")["test_recovered"])
            self.assertFalse((data / ".transition.json").exists())

    def test_fetch_cloud_pagination_hashes_attachments_and_changes(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            jira["issues"][0]["fields"]["assignee"]["name"] = self.env["JIRA_USER"]
            jira["issues"][0]["fields"]["assignee"]["self"] = jira["base"] + "/user"
            jira["issues"].append(raw_issue("DEMO-2"))
            check = self.command("fetch_jira_tasks.py", "--allow-http", "--check-connection")
            self.assertEqual(check["status"], "connected")
            self.assertFalse(self.data.exists())
            first = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(first["new_issue_keys"], ["DEMO-1", "DEMO-2"])
            task = read_json(self.data / "inbox" / "DEMO-1.json")
            self.assertEqual(len(task["comments"]), 2)
            attachment = self.data / task["attachments"][0]["local_path"]
            self.assertEqual(attachment.read_bytes(), b"synthetic attachment")
            self.assertTrue(attachment.resolve().is_relative_to(self.data.resolve()))
            self.assertNotIn("fake-signed-secret", json.dumps(task))
            self.assertNotIn("fake-account", json.dumps(task))
            self.assertNotIn(jira["base"], json.dumps(task))
            second = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(second["unchanged_issue_count"], 2)
            claim = self.claim()
            jira["attachment"] = b"changed bytes, unchanged metadata timestamp"
            third = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(third["updated_issue_keys"], ["DEMO-1", "DEMO-2"])
            self.assertEqual(attachment.read_bytes(), b"synthetic attachment")
            active = read_json(Path(claim["path"]))
            self.assertEqual(active["content_hash"], task["content_hash"])
            # Timestamp alone is also a changed version.
            jira["issues"][0]["fields"]["updated"] = "2026-01-02T00:00:00.000+0000"
            fourth = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(fourth["updated_issue_keys"], ["DEMO-1"])
            self.assertTrue(all(auth and auth.startswith("Basic ") for _, _, auth in jira["requests"]))

    def test_fetch_server_fallback_and_incomplete_download_errors(self):
        with mock_jira(api_version="2") as jira:
            self.env["JIRA_URL"] = jira["base"]
            jira["issues"].append(raw_issue("DEMO-2"))
            result = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(result["new_issue_keys"], ["DEMO-1", "DEMO-2"])
            jira["comments"][0]["body"] = "updated comment"
            jira["attachment_error"] = True
            error = self.command("fetch_jira_tasks.py", "--allow-http", expected=1)
            self.assertEqual(len(error["errors"]), 2)
            self.assertEqual(read_json(self.data / "inbox" / "DEMO-1.json")["comments"][0]["body"], "first")

    def test_fetch_filters_exact_statuses_even_with_custom_jql(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            names = ("NEW", "PAUSED", "TO DO", "REOPENED", " new ", "To Do",
                     "Open", "In Progress", "Done", "Closed", "NEW REVIEW")
            jira["issues"] = []
            for index, name in enumerate(names, 1):
                issue = raw_issue(f"DEMO-{index}")
                issue["fields"]["status"]["name"] = name
                jira["issues"].append(issue)
            result = self.command("fetch_jira_tasks.py", "--allow-http")
            expected_keys = [f"DEMO-{index}" for index in range(1, 7)]
            self.assertEqual(result["new_issue_keys"], expected_keys)
            self.assertEqual(result["skipped_status_issue_keys"],
                             [f"DEMO-{index}" for index in range(7, 12)])
            query = next(params["jql"][0] for path, params, _ in jira["requests"]
                         if path == "/rest/api/3/search/jql")
            self.assertEqual(query, 'assignee = currentUser() AND status IN '
                             '("NEW", "PAUSED", "TO DO", "REOPENED") ORDER BY updated ASC')
            self.assertEqual(sorted(task["issue_key"] for task in self.command("list_tasks.py")["tasks"]),
                             expected_keys)
            self.assertEqual(sum(path.endswith("/comment") for path, _, _ in jira["requests"]), 12)
            self.env["JIRA_JQL"] = "assignee = currentUser() ORDER BY updated ASC"
            repeated = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(repeated["unchanged_issue_count"], 6)
            self.assertEqual(repeated["skipped_status_issue_keys"], result["skipped_status_issue_keys"])
            self.assertEqual(len(list((self.data / "inbox").glob("*.json"))), 6)
            self.assertEqual(jira["writes"], [])

    def test_updated_completed_issue_only_returns_in_allowed_status(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            claim = self.claim()
            self.write_report(claim)
            self.owned("complete_task.py", claim)
            fields = jira["issues"][0]["fields"]
            fields["status"]["name"] = "In Progress"
            fields["updated"] = "2026-01-02T00:00:00.000+0000"
            result = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(result["updated_issue_keys"], [])
            self.assertEqual(result["skipped_status_issue_keys"], ["DEMO-1"])
            self.assertEqual(self.command("list_tasks.py")["status"], "empty")
            self.assertEqual(self.claim()["status"], "empty")
            fields["status"]["name"] = "REOPENED"
            fields["updated"] = "2026-01-03T00:00:00.000+0000"
            self.assertEqual(self.command("fetch_jira_tasks.py", "--allow-http")["updated_issue_keys"],
                             ["DEMO-1"])
            self.assertEqual(self.command("list_tasks.py")["tasks"][0]["jira_status"], "REOPENED")

    def test_refresh_hides_cached_pending_and_failed_missing_from_search(self):
        for api_version in ("2", "3"):
            with self.subTest(api_version=api_version), mock_jira(api_version) as jira:
                self.data = Path(self.temporary.name) / f"runtime-{api_version}"
                self.env["JIRA_URL"] = jira["base"]
                jira["issues"].append(raw_issue("DEMO-2"))
                self.command("fetch_jira_tasks.py", "--allow-http")
                claim = self.claim()
                failure = self.owned("fail_task.py", claim, "--error", "Preserve failure evidence")
                original_issues = deepcopy(jira["issues"])
                jira["issues"] = []
                self.command("fetch_jira_tasks.py", "--allow-http")
                self.assertEqual(self.command("list_tasks.py")["status"], "empty")
                self.assertEqual(self.claim()["status"], "empty")
                for key in ("DEMO-1", "DEMO-2"):
                    self.command("claim_next_task.py", "--issue-key", key, expected=1)
                    self.command("fail_task.py", "--retry", key, expected=1)
                self.assertTrue(Path(failure["path"]).exists())
                self.assertTrue((self.data / "inbox" / "DEMO-2.json").exists())
                jira["issues"] = original_issues
                self.command("fetch_jira_tasks.py", "--allow-http")
                self.assertEqual(self.command("list_tasks.py")["counts"], {"pending": 1, "failed": 1})

    def test_disallowed_status_hides_cached_issue_and_preserves_active_owner(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            claim = self.claim()
            jira["issues"][0]["fields"]["summary"] = "Updated while processing"
            self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(self.command("list_tasks.py")["counts"]["pending"], 1)
            active_before = Path(claim["path"]).read_bytes()
            jira["issues"][0]["fields"]["status"]["name"] = "In Progress"
            self.command("fetch_jira_tasks.py", "--allow-http")
            listing = self.command("list_tasks.py")
            self.assertEqual(listing["tasks"], [])
            self.assertEqual(listing["processing"]["issue_key"], claim["issue_key"])
            self.assertEqual(Path(claim["path"]).read_bytes(), active_before)
            self.write_report(claim)
            self.owned("complete_task.py", claim)
            self.assertEqual(self.command("list_tasks.py")["status"], "empty")

    def test_incomplete_search_does_not_hide_unseen_cached_tasks(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            jira["issues"].append(raw_issue("DEMO-2"))
            self.command("fetch_jira_tasks.py", "--allow-http")
            state_before = (self.data / "state.json").read_bytes()
            jira["search_error_at"] = 1
            result = self.command("fetch_jira_tasks.py", "--allow-http", expected=1)
            self.assertTrue(result["errors"])
            self.assertEqual((self.data / "state.json").read_bytes(), state_before)
            self.assertEqual(self.command("list_tasks.py")["counts"]["pending"], 2)

    def test_legacy_disallowed_or_missing_status_cannot_be_listed_claimed_or_retried(self):
        failed_task = snapshot("DEMO-1")
        self.enqueue(failed_task)
        claim = self.claim()
        failure = self.owned("fail_task.py", claim, "--error", "Legacy failure")
        task = read_json(Path(failure["path"]))
        task["status"] = {"name": "In Progress"}
        atomic_json(Path(failure["path"]), task)
        for key, status in (("DEMO-2", {"name": "Open"}), ("DEMO-3", None)):
            pending = snapshot(key)
            pending["status"] = status
            self.enqueue(pending)
        self.assertEqual(self.command("list_tasks.py")["status"], "empty")
        self.assertEqual(self.claim()["status"], "empty")
        for key in ("DEMO-1", "DEMO-2", "DEMO-3"):
            self.command("claim_next_task.py", "--issue-key", key, expected=1)
            self.command("fail_task.py", "--retry", key, expected=1)
        self.assertFalse(list((self.data / "processing").glob("*.json")))

    def test_attachment_size_and_missing_configuration(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            result = self.command("fetch_jira_tasks.py", "--allow-http",
                                  "--max-attachment-bytes", "1", expected=1)
            self.assertEqual(result["new_issue_keys"], [])
            self.assertEqual(self.claim()["status"], "empty")
        self.env.pop("JIRA_TOKEN")
        missing = self.command("fetch_jira_tasks.py", expected=1)
        self.assertEqual(missing["new_issue_keys"], [])
        self.assertTrue(missing["errors"])

    def test_snapshot_consistency_and_truncated_attachments(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            jira["declared_size"] = 999
            result = self.command("fetch_jira_tasks.py", "--allow-http", expected=1)
            self.assertIn("size differs", result["errors"][0]["error"])
            self.assertEqual(self.claim()["status"], "empty")
            jira["declared_size"] = None
            jira["changed_during_fetch"] = True
            result = self.command("fetch_jira_tasks.py", "--allow-http", expected=1)
            self.assertIn("changed during download", result["errors"][0]["error"])
            self.assertEqual(self.claim()["status"], "empty")

    def test_runtime_inside_skill_is_refused(self):
        with self.assertRaises(TaskError):
            Store(SCRIPTS.parent / ".jira-agent")

    def test_list_pending_and_failed_without_claiming_and_select_pending(self):
        self.assertEqual(self.command("list_tasks.py")["status"], "empty")
        self.enqueue(snapshot("DEMO-1"))
        failed_claim = self.claim()
        self.owned("fail_task.py", failed_claim, "--error", "Synthetic retry reason")
        pending = snapshot("DEMO-2")
        pending["status"] = {"name": "TO DO"}
        pending["priority"] = {"name": "High"}
        self.enqueue(pending)
        self.enqueue(snapshot("DEMO-3"))
        state_before = (self.data / "state.json").read_bytes()
        listing = self.command("list_tasks.py")
        self.assertEqual(listing["counts"], {"pending": 2, "failed": 1})
        tasks = {task["issue_key"]: task for task in listing["tasks"]}
        self.assertEqual(tasks["DEMO-1"]["queue_status"], "failed")
        self.assertEqual(tasks["DEMO-1"]["failure"]["error"], "Synthetic retry reason")
        self.assertEqual(tasks["DEMO-2"]["jira_status"], "TO DO")
        self.assertEqual(tasks["DEMO-2"]["priority"], "High")
        self.assertTrue(all(task["selectable"] for task in listing["tasks"]))
        self.assertNotIn("claim_token", json.dumps(listing))
        self.assertEqual((self.data / "state.json").read_bytes(), state_before)
        self.assertFalse(list((self.data / "processing").glob("*.json")))
        self.assertEqual(self.command("list_tasks.py", "--pending-only")["counts"], {"pending": 2, "failed": 0})
        selected = self.command("claim_next_task.py", "--issue-key", "DEMO-3")
        self.assertEqual(selected["issue_key"], "DEMO-3")
        self.assertTrue((self.data / "inbox" / "DEMO-2.json").exists())
        blocked = self.command("list_tasks.py")
        self.assertEqual(blocked["processing"]["issue_key"], "DEMO-3")
        self.assertFalse(blocked["can_claim"])
        self.assertTrue(all(not task["selectable"] for task in blocked["tasks"]))
        self.command("claim_next_task.py", "--issue-key", "DEMO-2", expected=1)

    def test_explicit_failed_selection_retries_with_new_owner(self):
        self.enqueue(snapshot())
        claim = self.claim()
        failure = self.owned("fail_task.py", claim, "--error", "Explicit retry test")
        self.assertEqual(self.claim()["status"], "empty")
        selected = self.command("claim_next_task.py", "--issue-key", "DEMO-1")
        task = read_json(Path(selected["path"]))
        self.assertEqual(selected["issue_key"], "DEMO-1")
        self.assertNotEqual(selected["claim_token"], claim["claim_token"])
        self.assertEqual(task["retry_history"][0]["error"], "Explicit retry test")
        self.assertNotIn("failure", task)
        self.assertFalse(Path(failure["path"]).exists())
        self.assertEqual(Path(selected["path"]).name, "DEMO-1.json")

    def test_list_prefers_latest_pending_and_excludes_completed_failures(self):
        self.enqueue(snapshot())
        claim = self.claim()
        self.owned("fail_task.py", claim, "--error", "Old failed version")
        self.enqueue(snapshot(content="newer"))
        listing = self.command("list_tasks.py")
        self.assertEqual(listing["counts"], {"pending": 1, "failed": 0})
        claim = self.command("claim_next_task.py", "--issue-key", "DEMO-1")
        self.assertEqual(read_json(Path(claim["path"]))["content_hash"], digest("newer"))
        self.write_report(claim)
        self.owned("complete_task.py", claim)
        self.assertEqual(self.command("list_tasks.py")["status"], "empty")
        self.command("claim_next_task.py", "--issue-key", "DEMO-1", expected=1)

    def test_unavailable_selection_does_not_claim_another_issue(self):
        self.enqueue(snapshot("DEMO-1"))
        self.command("claim_next_task.py", "--issue-key", "DEMO-999", expected=1)
        self.command("claim_next_task.py", "--issue-key", "../unsafe", expected=1)
        self.command("claim_next_task.py", "--issue-key", "DEMO-1",
                     "--recover-stale", "DEMO-1", "--confirm-owner-stopped", expected=1)
        self.assertFalse(list((self.data / "processing").glob("*.json")))
        self.assertTrue((self.data / "inbox" / "DEMO-1.json").exists())

    def test_listing_does_not_replay_a_pending_jira_write(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            jira["label_error"] = 403
            self.command("claim_next_task.py", "--issue-key", "DEMO-1",
                         "--allow-http", labels=True, expected=1)
            writes = len(jira["writes"])
            requests = len(jira["requests"])
            journal = (self.data / ".transition.json").read_bytes()
            listing = self.command("list_tasks.py", expected=1)
            self.assertEqual(listing["status"], "blocked")
            self.assertTrue(listing["transition_pending"])
            self.assertFalse(listing["can_claim"])
            self.assertEqual(len(jira["writes"]), writes)
            self.assertEqual(len(jira["requests"]), requests)
            self.assertEqual((self.data / ".transition.json").read_bytes(), journal)

    def test_selected_failed_issue_updates_jira_processing_label(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            claim = self.command("claim_next_task.py", "--issue-key", "DEMO-1",
                                 "--allow-http", labels=True)
            self.owned("fail_task.py", claim, "--allow-http", "--error", "Choose this again", labels=True)
            self.assertEqual(self.command("list_tasks.py")["counts"]["failed"], 1)
            selected = self.command("claim_next_task.py", "--issue-key", "DEMO-1",
                                    "--allow-http", labels=True)
            self.assertEqual(set(jira["issues"][0]["fields"]["labels"]), {"offline", "ai-processing"})
            self.assertEqual(read_json(Path(selected["path"]))["retry_history"][0]["error"], "Choose this again")

    def test_claim_token_is_safe_as_a_separate_cli_argument(self):
        self.enqueue(snapshot())
        with patch("claim_next_task.secrets.token_urlsafe", return_value="-leading-dash"):
            with patch.object(sys, "argv", ["claim_next_task.py", "--data-dir",
                                           str(self.data), "--no-jira-labels"]):
                claim = claim_main()
        self.assertTrue(claim["claim_token"].startswith("claim_"))
        self.owned("fail_task.py", claim, "--error", "CLI-safe owner token")

    def test_exclude_pending_permanently_hides_and_archives_without_analysis(self):
        self.enqueue(snapshot("DEMO-1"))
        self.enqueue(snapshot("DEMO-2"))
        result = self.command("exclude_task.py", "--issue-key", "DEMO-1")
        self.assertEqual(result["status"], "excluded")
        exclusion = result["exclusion"]
        self.assertEqual(exclusion["reason"], "Won't do")
        self.assertFalse((self.data / "inbox" / "DEMO-1.json").exists())
        self.assertTrue((self.data / exclusion["snapshot_path"]).exists())
        self.assertEqual(self.enqueue(snapshot("DEMO-1", content="changed")), "excluded")
        self.command("claim_next_task.py", "--issue-key", "DEMO-1", expected=1)
        self.assertEqual(self.command("list_tasks.py")["counts"], {"pending": 1, "failed": 0})
        listing = self.command("list_tasks.py", "--include-excluded")
        excluded = next(task for task in listing["tasks"] if task["issue_key"] == "DEMO-1")
        self.assertEqual(excluded["queue_status"], "excluded")
        self.assertFalse(excluded["selectable"])
        self.assertEqual(excluded["exclusion_reason"], "Won't do")
        self.assertEqual(listing["excluded_issue_count"], 1)
        repeated = self.command("exclude_task.py", "--issue-key", "DEMO-1")
        self.assertEqual(repeated["status"], "already_excluded")
        self.assertEqual(repeated["exclusion"], exclusion)
        self.assertEqual(self.claim()["issue_key"], "DEMO-2")

    def test_exclude_failed_blocks_retry_and_keeps_failure_evidence(self):
        self.enqueue(snapshot())
        claim = self.claim()
        failure = self.owned("fail_task.py", claim, "--error", "Retain this diagnostic")
        self.command("exclude_task.py", "--issue-key", "DEMO-1", "--reason", "Not my project")
        self.assertTrue(Path(failure["path"]).exists())
        self.assertEqual(self.command("list_tasks.py")["status"], "empty")
        self.command("fail_task.py", "--retry", "DEMO-1", expected=1)
        self.command("claim_next_task.py", "--issue-key", "DEMO-1", expected=1)
        self.command("exclude_task.py", "--issue-key", "DEMO-1", "--restore")
        self.assertEqual(self.command("list_tasks.py")["status"], "empty")
        self.command("fail_task.py", "--retry", "DEMO-1", expected=1)
        self.assertEqual(self.enqueue(snapshot()), "updated")
        self.assertEqual(self.claim()["status"], "claimed")

    def test_excluded_jira_issue_skips_comments_attachments_and_all_future_updates(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("exclude_task.py", "--issue-key", "DEMO-1")
            self.assertEqual(jira["requests"], [])
            self.assertEqual(jira["writes"], [])
            result = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(result["new_issue_keys"], [])
            self.assertEqual(result["excluded_issue_keys"], ["DEMO-1"])
            jira["issues"][0]["fields"]["summary"] = "Changed after exclusion"
            jira["issues"][0]["fields"]["updated"] = "2026-01-02T00:00:00.000+0000"
            jira["attachment"] = b"new content"
            result = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(result["updated_issue_keys"], [])
            self.assertEqual(result["excluded_issue_keys"], ["DEMO-1"])
            self.assertFalse(any("/comment" in path or "/attachment/" in path for path, _, _ in jira["requests"]))
            self.assertFalse(list((self.data / "attachments").rglob("*")))
            self.assertEqual(self.command("list_tasks.py")["status"], "empty")
            restore = self.command("exclude_task.py", "--issue-key", "DEMO-1", "--restore")
            self.assertTrue(restore["needs_fetch"])
            self.assertEqual(jira["writes"], [])
            self.command("claim_next_task.py", "--issue-key", "DEMO-1", expected=1)
            fetched = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(fetched["new_issue_keys"], ["DEMO-1"])
            self.assertEqual(read_json(self.data / "inbox" / "DEMO-1.json")["summary"], "Changed after exclusion")

    def test_restore_forces_unchanged_completed_issue_to_be_fetched_again(self):
        self.enqueue(snapshot())
        claim = self.claim()
        self.write_report(claim)
        self.owned("complete_task.py", claim)
        original_completed = read_json(self.data / "state.json")["issues"]["DEMO-1"]["last_completed"]
        self.command("exclude_task.py", "--issue-key", "DEMO-1")
        self.command("exclude_task.py", "--issue-key", "DEMO-1", "--restore")
        self.assertEqual(self.enqueue(snapshot()), "updated")
        state = read_json(self.data / "state.json")["issues"]["DEMO-1"]
        self.assertNotIn("requeue_requested", state)
        self.assertNotIn("excluded", state)
        self.assertEqual(state["last_completed"], original_completed)
        self.assertEqual(state["last_exclusion"]["reason"], "Won't do")
        self.assertEqual(self.claim()["issue_key"], "DEMO-1")

    def test_exclusion_refuses_active_issue_and_does_not_replay_live_transition(self):
        self.enqueue(snapshot())
        claim = self.claim()
        self.command("exclude_task.py", "--issue-key", "DEMO-1", expected=1)
        self.assertTrue(Path(claim["path"]).exists())
        self.assertNotIn("excluded", read_json(self.data / "state.json")["issues"]["DEMO-1"])
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.data = Path(self.temporary.name) / "pending-live-label"
            self.command("fetch_jira_tasks.py", "--allow-http")
            jira["label_error"] = 403
            self.command("claim_next_task.py", "--allow-http", labels=True, expected=1)
            requests, writes = len(jira["requests"]), len(jira["writes"])
            journal = (self.data / ".transition.json").read_bytes()
            self.command("exclude_task.py", "--issue-key", "DEMO-1", expected=1)
            self.assertEqual(len(jira["requests"]), requests)
            self.assertEqual(len(jira["writes"]), writes)
            self.assertEqual((self.data / ".transition.json").read_bytes(), journal)

    def test_interrupted_local_exclusion_can_be_retried_without_jira(self):
        self.enqueue(snapshot())
        state = read_json(self.data / "state.json")
        destination = self.data / "excluded" / "DEMO-1-offline.json"
        state["issues"]["DEMO-1"]["excluded"] = {
            "excluded_at": datetime.now(timezone.utc).isoformat(), "reason": "Won't do",
            "summary": "Synthetic issue", "updated": snapshot()["updated"],
            "snapshot_path": str(destination.relative_to(self.data)),
        }
        atomic_json(self.data / ".transition.json", {
            "source": str(Path("inbox") / "DEMO-1.json"),
            "destination": str(destination.relative_to(self.data)), "state": state,
        })
        result = self.command("exclude_task.py", "--issue-key", "DEMO-1")
        self.assertEqual(result["status"], "already_excluded")
        self.assertTrue(destination.exists())
        self.assertFalse((self.data / ".transition.json").exists())
        self.assertEqual(self.command("list_tasks.py")["status"], "empty")

    def test_exclusion_validates_inputs_and_redacts_reasons(self):
        self.command("exclude_task.py", "--issue-key", "../unsafe", expected=1)
        self.command("exclude_task.py", "--issue-key", "DEMO-1", "--reason", " ", expected=1)
        self.command("exclude_task.py", "--issue-key", "DEMO-1",
                     "--restore", "--reason", "Invalid combination", expected=1)
        self.command("exclude_task.py", "--issue-key", "DEMO-1", "--restore", expected=1)
        result = self.command("exclude_task.py", "--issue-key", "DEMO-1",
                              "--reason", "fake-token-not-real fake-account")
        self.assertNotIn("fake-token-not-real", json.dumps(result))
        self.assertNotIn("fake-account", (self.data / "state.json").read_text(encoding="utf-8"))

    def test_jira_label_lifecycle_preserves_other_labels_and_does_not_requeue(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            fields = jira["issues"][0]["fields"]
            fields["labels"] = ["offline", "business", "ai-failed"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            claim = self.command("claim_next_task.py", "--allow-http", labels=True)
            self.assertEqual(set(fields["labels"]), {"offline", "business", "ai-processing"})
            repeated = self.command("claim_next_task.py", "--allow-http", labels=True)
            self.assertEqual(repeated["status"], "already_processing")
            self.assertEqual(len(jira["writes"]), 1)
            self.assertEqual(self.command("fetch_jira_tasks.py", "--allow-http")["unchanged_issue_count"], 1)
            self.owned("complete_task.py", claim, "--allow-http", labels=True, expected=1)
            self.assertEqual(len(jira["writes"]), 1)
            self.write_report(claim)
            self.owned("complete_task.py", claim, "--allow-http", labels=True)
            self.assertEqual(set(fields["labels"]), {"offline", "business", "ai-processed"})
            self.assertEqual(self.command("fetch_jira_tasks.py", "--allow-http")["unchanged_issue_count"], 1)
            self.assertEqual(self.claim()["status"], "empty")
            for payload in jira["writes"]:
                self.assertEqual(set(payload), {"update"})
                self.assertEqual(set(payload["update"]), {"labels"})
            # A real timestamp-only update is still detected.
            fields["updated"] = "2026-01-02T00:00:00.000+0000"
            self.assertEqual(self.command("fetch_jira_tasks.py", "--allow-http")["updated_issue_keys"], ["DEMO-1"])
            next_claim = self.command("claim_next_task.py", "--allow-http", labels=True)
            self.assertIn("ai-processing", fields["labels"])
            self.assertNotIn("ai-processed", fields["labels"])
            self.owned("fail_task.py", next_claim, "--allow-http", "--error", "Synthetic failure", labels=True)
            self.assertEqual(set(fields["labels"]), {"offline", "business", "ai-failed"})
            self.assertEqual(self.command("fetch_jira_tasks.py", "--allow-http")["unchanged_issue_count"], 1)
            self.command("fail_task.py", "--retry", "DEMO-1", "--allow-http", labels=True)
            self.assertEqual(set(fields["labels"]), {"offline", "business"})

    def test_label_failure_blocks_completion_and_replays_safely(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            jira["label_error"] = 403
            self.command("claim_next_task.py", "--allow-http", labels=True, expected=1)
            self.assertTrue((self.data / ".transition.json").exists())
            self.assertFalse(list((self.data / "processing").glob("*.json")))
            jira["label_error"] = None
            claim = self.command("claim_next_task.py", "--allow-http", labels=True)
            self.assertEqual(claim["status"], "claimed")
            self.write_report(claim)
            # Simulate a lost response after Jira accepted the label write.
            jira["label_error_after_apply"] = True
            self.owned("complete_task.py", claim, "--allow-http", labels=True, expected=1)
            self.assertTrue(Path(claim["path"]).exists())
            self.assertFalse(list((self.data / "done").glob("*.json")))
            writes_before_retry = len(jira["writes"])
            jira["label_error_after_apply"] = False
            result = self.owned("complete_task.py", claim, "--allow-http", labels=True)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(jira["writes"]), writes_before_retry)
            self.assertEqual(self.command("fetch_jira_tasks.py", "--allow-http")["unchanged_issue_count"], 1)
            self.assertFalse((self.data / ".transition.json").exists())

    def test_concurrent_content_edit_is_not_hidden_by_label_update(self):
        with mock_jira() as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            jira["edit_during_label_write"] = True
            claim = self.command("claim_next_task.py", "--allow-http", labels=True)
            result = self.command("fetch_jira_tasks.py", "--allow-http")
            self.assertEqual(result["updated_issue_keys"], ["DEMO-1"])
            self.assertEqual(read_json(Path(claim["path"]))["summary"], "Synthetic attachment test")
            self.assertEqual(read_json(self.data / "inbox" / "DEMO-1.json")["summary"], "Concurrent user edit")

    def test_stale_recovery_clears_processing_label(self):
        with mock_jira(api_version="2") as jira:
            self.env["JIRA_URL"] = jira["base"]
            self.command("fetch_jira_tasks.py", "--allow-http")
            claim = self.command("claim_next_task.py", "--allow-http", labels=True)
            task = read_json(Path(claim["path"]))
            task["claim"]["claimed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            atomic_json(Path(claim["path"]), task)
            self.command("claim_next_task.py", "--recover-stale", "DEMO-1",
                         "--confirm-owner-stopped", "--allow-http", labels=True)
            self.assertEqual(jira["issues"][0]["fields"]["labels"], ["offline"])

    def test_cross_origin_redirect_strips_authentication(self):
        redirect = SafeRedirect()
        request = Request("https://jira.example/attachment", headers={
            "Authorization": "Basic fake-header", "Cookie": "fake-cookie", "Accept": "*/*"})
        redirected = redirect.redirect_request(
            request, None, 302, "", {}, "https://cdn.example/file")
        self.assertFalse(redirected.has_header("Authorization"))
        self.assertFalse(redirected.has_header("Cookie"))
        with self.assertRaises(TaskError):
            redirect.redirect_request(request, None, 302, "", {}, "http://cdn.example/file")
        with patch.dict(os.environ, {"JIRA_USER": "fake-account", "JIRA_TOKEN": "fake-token"}):
            self.assertEqual(redact("fake-token"), "[REDACTED]")


class SkillStructureTests(unittest.TestCase):
    def test_required_structure_and_frontmatter(self):
        skill = SCRIPTS.parent
        for relative in ("SKILL.md", "references/workflow.md",
                         "scripts/fetch_jira_tasks.py", "scripts/claim_next_task.py",
                         "scripts/complete_task.py", "scripts/fail_task.py", "scripts/list_tasks.py",
                         "scripts/exclude_task.py"):
            self.assertTrue((skill / relative).is_file(), relative)
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)
        self.assertEqual(frontmatter[0], "")
        fields = dict(line.split(": ", 1) for line in frontmatter[1].strip().splitlines())
        self.assertEqual(set(fields), {"name", "description"})
        self.assertEqual(fields["name"], "jira-task-processor")
        self.assertGreater(len(fields["description"]), 20)
        self.assertNotIn(": ", fields["description"])
        for directory in Store.directories:
            self.assertFalse((skill / directory).exists())
        self.assertFalse((skill / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
