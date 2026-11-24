"""Resumption, live-data changes and fail-closed completeness; no network or secrets."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audit import run_inventory
from stepik import APIError, Stepik
from test_integration import StepikTransport


class AuditAPI:
    def __init__(self):
        self.user_id = None
        self.account = 42
        self.calls = []
        self.interrupt_history = None
        self.interrupt_section = None
        self.error_section = None
        self.history = [
            [{"id": 301, "attempt": 11, "status": "correct", "reply": "PRIVATE_ANSWER"}],
            [{"id": 302, "attempt": 12, "status": "settled"}],
            [{"id": 303, "attempt": 13, "status": "correct"}],
        ]
        self.attempts = {11: {"id": 11, "user": 42, "step": 90},
                         12: {"id": 12, "user": 42, "step": 91},
                         13: {"id": 13, "user": 42, "step": 92}}
        self.resources = {
            "sections": [{"id": 10, "units": [20]}, {"id": 11, "units": [21]}],
            "units": [{"id": 20, "lesson": 30}, {"id": 21, "lesson": 31}],
            "lessons": [{"id": 30, "steps": [1, 2], "update_date": "2026-11-22T00:00:00Z"},
                        {"id": 31, "steps": [3], "update_date": "2026-11-22T00:00:00Z"}],
            "steps": [{"id": sid, "block": {"name": "choice", "text": "PRIVATE_TASK_CONTENT"},
                       "progress": {"is_passed": False}} for sid in (1, 2, 3)]}
        self.step_histories = {}

    def identity(self):
        self.user_id = self.account
        return self.account

    def courses(self):
        return [{"id": 5, "title": "Course", "sections": [10, 11]}]

    def get(self, resource, **params):
        self.calls.append((resource, copy.deepcopy(params)))
        if resource == "submissions":
            page = params["page"]
            rows = self.history[page - 1]
            return {"submissions": copy.deepcopy(rows), "meta": {"page": page, "has_next": page < len(self.history)}}
        kind, key = resource.split("/")
        rows = [x for x in self.resources[kind] if x["id"] == int(key)]
        if not rows:
            raise APIError("stepik", 404)
        return {kind: copy.deepcopy(rows)}

    def fetch_many(self, kind, ids):
        ids = list(dict.fromkeys(ids))
        self.calls.append((kind, tuple(ids)))
        if kind == "attempts":
            if self.interrupt_history in ids:
                raise KeyboardInterrupt()
            return [copy.deepcopy(self.attempts[i]) for i in ids]
        if kind == "units" and 21 in ids:
            if self.interrupt_section:
                raise KeyboardInterrupt()
            if self.error_section:
                raise APIError("stepik", 500)
        if kind == "progresses":
            return []
        return [copy.deepcopy(x) for i in ids for x in self.resources[kind] if x["id"] == i]

    def pages(self, kind, **params):
        self.calls.append(("step_history", params["step"]))
        return iter(copy.deepcopy(self.step_histories.get(params["step"], [])))


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "progress.json"
        stdout = patch("sys.stdout", new_callable=io.StringIO)
        stdout.start()
        self.addCleanup(stdout.stop)

    def run_audit(self, api, **kwargs):
        return run_inventory(api, self.path, expected_user=api.account, **kwargs)

    def report(self):
        return json.loads((self.path.parent / "reports/latest-dry-run.json").read_text())

    def test_interrupt_history_resumes_without_fetching_verified_attempt_pages(self):
        first = AuditAPI()
        first.interrupt_history = 12
        with self.assertRaises(KeyboardInterrupt):
            self.run_audit(first)
        self.assertFalse(self.report()["inventory_complete"])
        second = AuditAPI()
        result = self.run_audit(second)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["history_inventory"]["history_resumed_at"], 2)
        self.assertNotIn(("attempts", (11,)), second.calls)
        self.assertTrue(any(kind == "attempts" and 12 in ids for kind, ids in second.calls))
        self.assertEqual(result["candidates"], 3)

    def test_interrupt_after_section_preserves_sections_and_completed_history(self):
        first = AuditAPI()
        first.interrupt_section = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_audit(first)
        second = AuditAPI()
        result = self.run_audit(second)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["resumed_sections"], 1)
        self.assertFalse(any(kind == "attempts" and ids for kind, ids in second.calls))
        pages = [p["page"] for kind, p in second.calls if kind == "submissions"]
        self.assertNotIn(2, pages)  # Only first/last anchors, not the interior archive.

    def test_head_change_invalidates_old_pagination(self):
        first = AuditAPI()
        first.interrupt_history = 13
        with self.assertRaises(KeyboardInterrupt):
            self.run_audit(first)
        second = AuditAPI()
        second.history[0][0]["status"] = "settled"
        result = self.run_audit(second)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["history_inventory"]["history_invalidations"], 1)
        self.assertIn(("attempts", (11,)), second.calls)

    def test_interior_verdict_change_is_caught_by_fresh_candidate_history(self):
        self.run_audit(AuditAPI())
        second = AuditAPI()
        second.history[1][0]["status"] = "correct"  # Anchors deliberately unchanged.
        second.attempts[12]["step"] = 1
        second.step_histories[1] = [{"id": 302, "attempt": 12, "status": "correct"}]
        result = self.run_audit(second)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["reasons"]["prior_correct_submission"], 1)

    def test_changed_child_without_parent_timestamp_change_is_rechecked(self):
        first = AuditAPI()
        first.resources["steps"][0]["block"]["name"] = "video"
        self.assertEqual(self.run_audit(first)["candidates"], 2)
        result = self.run_audit(AuditAPI())
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["candidates"], 3)

    def test_changed_progress_removes_cached_candidate(self):
        self.run_audit(AuditAPI())
        second = AuditAPI()
        second.resources["steps"][0]["progress"] = {"is_passed": True}
        result = self.run_audit(second)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["reasons"]["already_solved"], 1)

    def test_exams_closed_units_and_unsupported_are_expected_exclusions(self):
        api = AuditAPI()
        api.resources["sections"][1]["is_exam"] = True
        api.resources["steps"][0]["block"]["name"] = "video"
        result = self.run_audit(api)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["read_errors"], [])
        self.assertEqual(result["reasons"]["exam"], 1)
        self.assertEqual(result["reasons"]["unsupported_type"], 1)
        api = AuditAPI()
        api.resources["units"][1]["is_active"] = False
        result = self.run_audit(api)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["reasons"]["unit_unavailable"], 1)

    def test_unresolved_api_errors_are_not_availability_and_can_resume(self):
        api = AuditAPI()
        api.error_section = True
        result = self.run_audit(api)
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(result["stop_reason"], "unresolved_read_errors")
        self.assertEqual(len(result["read_errors"]), 1)
        recovered = self.run_audit(AuditAPI())
        self.assertTrue(recovered["inventory_complete"])
        self.assertEqual(recovered["candidates"], 3)

    def test_malformed_availability_is_a_schema_error_not_a_closed_section(self):
        api = AuditAPI()
        api.resources["sections"][0]["end_date"] = "bad-date"
        with self.assertRaises(APIError):
            self.run_audit(api)
        report = self.report()
        self.assertFalse(report["inventory_complete"])
        self.assertTrue(report["read_errors"])
        self.assertNotIn("section_unavailable", report["reasons"])

    def test_confirmed_403_is_expected_but_transport_failure_is_not(self):
        api = AuditAPI()
        original = api.fetch_many
        def denied(kind, ids):
            if kind == "sections":
                raise APIError("stepik", 403)
            return original(kind, ids)
        api.fetch_many = denied
        original_get = api.get
        def denied_get(resource, **params):
            if resource.startswith("sections/"):
                raise APIError("stepik", 403)
            return original_get(resource, **params)
        api.get = denied_get
        result = self.run_audit(api)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["reasons"]["resource_unavailable"], 2)

    def test_account_and_parameters_are_bound_without_overwriting_checkpoint(self):
        self.run_audit(AuditAPI())
        other = AuditAPI()
        other.account = 43
        with self.assertRaisesRegex(ValueError, "account_or_parameters_mismatch"):
            self.run_audit(other)
        with self.assertRaisesRegex(ValueError, "account_or_parameters_mismatch"):
            self.run_audit(AuditAPI(), max_steps=200000)
        self.assertTrue(self.run_audit(AuditAPI())["inventory_complete"])

    def test_private_checkpoint_and_reports_have_no_answers_or_task_content(self):
        self.run_audit(AuditAPI())
        db = self.path.parent / "audit.sqlite3"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.exists())  # Submission journal was never created.
        for p in self.path.parent.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                self.assertNotIn(b"PRIVATE_ANSWER", data)
                self.assertNotIn(b"PRIVATE_TASK_CONTENT", data)

    def test_real_client_wire_audit_is_get_only_and_never_calls_model(self):
        transport = StepikTransport()
        api = Stepik("OFFLINE_ID", "OFFLINE_SECRET", session=transport)
        result = run_inventory(
            api, self.path, expected_user=42)
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["candidates"], 7)
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))
        with self.assertRaisesRegex(ValueError, "read_only_stepik_client_cannot_write"):
            api.create_attempt(1)
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))

    def test_attempt_batches_use_100_ids_only_for_read_only_clients(self):
        for read_only, expected_calls in ((False, 5), (True, 1)):
            api = Stepik("fake", "fake")
            api.read_only = read_only
            calls = []
            def request(method, resource, **kwargs):
                ids = [v for k, v in kwargs["params"] if k == "ids[]"]
                calls.append(ids)
                return {"attempts": [{"id": i} for i in ids], "meta": {"has_next": False}}
            api.request = request
            rows = api.fetch_many("attempts", list(range(1, 101)))
            self.assertEqual(len(rows), 100)
            self.assertEqual(len(calls), expected_calls)

    def test_attempt_batch_handles_server_pagination_without_omitting_ids(self):
        api = Stepik("fake", "fake")
        api.read_only = True
        calls = []
        def request(method, resource, **kwargs):
            params = kwargs["params"]
            ids = [v for k, v in params if k == "ids[]"]
            page = dict(params)["page"]
            part = ids[(page - 1) * 20:page * 20]
            calls.append(page)
            return {"attempts": [{"id": i} for i in part], "meta": {"has_next": page * 20 < len(ids)}}
        api.request = request
        rows = api.fetch_many("attempts", list(range(1, 101)))
        self.assertEqual([r["id"] for r in rows], list(range(1, 101)))
        self.assertEqual(calls, [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
