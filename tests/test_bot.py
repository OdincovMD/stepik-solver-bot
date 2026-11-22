import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bot import count_today, full_score, run, safe_error
from config import load_env
from solver import ChatModel, validate_reply
from stepik import APIError, Stepik, is_solved
from storage import load_state, save_state, state_lock

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
DAY = NOW.date().isoformat()


def task(sid=1, kind="choice"):
    return {"id": sid, "block": {"name": kind, "text": "Question"}, "progress": {"is_passed": False}}


class FakeApi:
    def __init__(self, statuses=("correct",)):
        self.tasks = [task(i + 1) for i in range(len(statuses))]
        self.statuses = statuses
        self.sent, self.rows, self.created = [], {}, []
        self.solved, self.old = set(), set()
        self.notices = []
        self.timeout_after_write = self.progress_delay = self.verdict_delay = False
        self.server_time, self.score = NOW.isoformat(), "1"

    def identity(self):
        return 42

    def courses(self):
        return [{"id": 5}]

    def steps(self, courses):
        for item in self.tasks:
            yield copy.deepcopy(item), courses[0]

    def step(self, sid):
        value = copy.deepcopy(self.tasks[sid - 1])
        if sid in self.solved and not self.progress_delay:
            value["progress"] = {"is_passed": True}
        return value

    def correct_history(self, sid):
        return [{"id": 999, "status": "correct"}] if sid in self.old else []

    def create_attempt(self, sid):
        self.created.append(sid)
        return {"id": sid * 10, "step": sid, "user": 42,
                "dataset": {"options": ["A", "B"], "is_multiple_choice": False}}

    def submit(self, aid, reply):
        sid = aid // 10
        self.sent.append((aid, reply))
        self.rows[aid] = {"id": aid + 1, "attempt": aid, "reply": reply,
                          "status": self.statuses[sid - 1], "score": self.score, "time": self.server_time}
        if self.statuses[sid - 1] == "correct":
            self.solved.add(sid)
        if self.timeout_after_write:
            raise APIError("stepik_transport")
        return aid + 1

    def verdict(self, subid):
        if self.verdict_delay:
            raise APIError("verdict_timeout")
        return self.rows[subid - 1]

    def attempt_submissions(self, aid):
        return [self.rows[aid]] if aid in self.rows else []


class FakeSolver:
    def solve(self, step, attempt):
        return {"choices": [True, False]}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "progress.json"
        for context in (patch("bot.utcnow", return_value=NOW), patch("sys.stdout", new_callable=io.StringIO)):
            context.start()
            self.addCleanup(context.stop)

    def run_bot(self, api, **kw):
        return run(api, kw.pop("solver", FakeSolver()), self.path, expected_user=42, dry_run=False, **kw)

    def report(self, dry=False):
        return json.loads((self.path.parent / "reports" / ("latest-dry-run.json" if dry else "latest.json")).read_text())

    def test_counts_only_correct_new_tasks_and_restarts(self):
        api = FakeApi(["wrong", "correct", "correct", "correct"])
        self.assertEqual(self.run_bot(api, target=2), 2)
        self.assertEqual([x[0] for x in api.sent], [10, 20, 30])
        self.assertEqual(self.run_bot(api, target=2), 2)
        self.assertEqual(len(api.sent), 3)
        self.assertEqual(self.report()["stop_reason"], "target_reached")

    def test_dry_run_no_attempts_model_or_progress_write(self):
        api = FakeApi(["correct", "correct"])
        with patch.object(FakeSolver, "solve", side_effect=AssertionError()):
            self.assertEqual(run(api, FakeSolver(), self.path), 0)
        self.assertFalse(self.path.exists())
        self.assertFalse(api.created)
        self.assertEqual(self.report(True)["candidates"], 2)

    def test_unknown_and_malformed_progress_skipped(self):
        for progress in (None, {}, {"score": "0"}, {"is_passed": None}, {"is_passed": "false"}):
            with self.subTest(progress=progress):
                api = FakeApi()
                api.tasks[0]["progress"] = progress
                self.assertEqual(self.run_bot(api), 0)
                self.assertFalse(api.sent)

    def test_prior_correct_history_overrides_false_progress(self):
        api = FakeApi()
        api.old.add(1)
        self.assertEqual(self.run_bot(api), 0)
        self.assertFalse(api.created)

    def test_partial_score_not_counted_or_canary(self):
        api = FakeApi()
        api.score = "0.5"
        self.assertEqual(self.run_bot(api, canary=True), 0)
        self.assertNotIn("canary", load_state(self.path))
        self.assertEqual(self.report()["reasons"], {"partial_score": 1})

    def test_lost_post_response_recovered_without_resend(self):
        api = FakeApi(["correct", "correct"])
        api.timeout_after_write = True
        self.assertEqual(self.run_bot(api, target=1), 0)
        self.assertEqual(self.report()["stop_reason"], "unresolved_submission")
        self.assertEqual(self.run_bot(api, target=1), 1)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.report()["reasons"], {"recovered_accepted": 1})

    def test_verdict_timeout_reconciles_persisted_id(self):
        api = FakeApi()
        api.verdict_delay = True
        self.run_bot(api)
        self.assertEqual(load_state(self.path)["jobs"]["1"]["submission_id"], 11)
        api.verdict_delay = False
        self.assertEqual(self.run_bot(api, target=1), 1)
        self.assertEqual(len(api.sent), 1)

    def test_empty_reconciliation_remains_blocked(self):
        api = FakeApi(["correct", "correct"])
        api.timeout_after_write = True
        self.run_bot(api)
        api.rows.clear()
        self.assertEqual(self.run_bot(api), 0)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.report()["stop_reason"], "unresolved_submission")

    def test_delayed_progress_never_increments_early(self):
        api = FakeApi()
        api.progress_delay = True
        self.assertEqual(self.run_bot(api, canary=True, model_digest="digest"), 0)
        self.assertFalse(load_state(self.path)["solved"])
        api.progress_delay = False
        self.assertEqual(self.run_bot(api, target=1), 1)
        self.assertEqual(load_state(self.path)["canary"]["model_digest"], "digest")

    def test_yesterday_result_recovered_but_not_counted_today(self):
        api = FakeApi()
        yesterday = NOW - timedelta(days=1)
        api.server_time, api.verdict_delay = yesterday.isoformat(), True
        with patch("bot.utcnow", return_value=yesterday):
            self.run_bot(api)
        api.verdict_delay = False
        self.assertEqual(self.run_bot(api), 0)
        self.assertEqual(load_state(self.path)["solved"]["1"]["date"], "2026-11-20")
        self.assertEqual(len(api.sent), 1)

    def test_boundary_after_model_prevents_submission(self):
        api = FakeApi()
        with patch("bot.can_start", side_effect=[True, False]):
            self.run_bot(api)
        self.assertEqual(api.created, [1])
        self.assertFalse(api.sent)

    def test_near_midnight_guard(self):
        api = FakeApi()
        with patch("bot.utcnow", return_value=NOW.replace(hour=23, minute=50)):
            self.run_bot(api)
        self.assertFalse(api.created)
        self.assertEqual(self.report()["stop_reason"], "utc_day_boundary_guard")

    def test_changed_progress_after_model_is_skipped(self):
        api = FakeApi()
        class RacingSolver(FakeSolver):
            def solve(self, step, attempt):
                api.old.add(1)
                return super().solve(step, attempt)
        self.run_bot(api, solver=RacingSolver())
        self.assertFalse(api.sent)

    def test_canary_sends_one_even_when_wrong(self):
        api = FakeApi(["wrong", "correct"])
        self.run_bot(api, canary=True)
        self.assertEqual(len(api.sent), 1)
        self.assertNotIn("canary", load_state(self.path))

    def test_daily_requires_verified_canary_for_model(self):
        api = FakeApi(["correct", "correct"])
        with self.assertRaisesRegex(RuntimeError, "verified_canary_required"):
            self.run_bot(api, require_canary=True, model_digest="local")
        self.assertFalse(api.sent)
        self.run_bot(api, canary=True, model_digest="local")
        self.assertEqual(self.run_bot(api, require_canary=True, model_digest="local", target=2), 2)

    def test_wrong_or_missing_expected_account_blocks_writes(self):
        api = FakeApi()
        for expected in (999, None):
            with self.assertRaises(RuntimeError):
                run(api, FakeSolver(), self.path, dry_run=False, expected_user=expected)
        self.assertFalse(api.created)

    def test_state_cannot_be_shared_between_accounts(self):
        state = load_state(self.path)
        state["account"] = 999
        save_state(self.path, state)
        with self.assertRaisesRegex(RuntimeError, "state_belongs"):
            self.run_bot(FakeApi())

    def test_legacy_counts_are_unverified(self):
        save_state(self.path, {"solved": {"1": {"date": DAY}}, "attempted": {}})
        self.assertEqual(count_today(load_state(self.path), DAY), 0)
        api = FakeApi()
        self.assertEqual(self.run_bot(api), 0)
        self.assertFalse(api.sent)

    def test_state_corruption_fails_closed(self):
        self.path.write_text("broken json")
        api = FakeApi()
        with self.assertRaises(ValueError):
            self.run_bot(api)
        self.assertEqual(self.path.read_text(), "broken json")
        self.assertFalse(api.created)

    def test_lock_prevents_concurrent_writers(self):
        with state_lock(self.path.parent):
            with self.assertRaisesRegex(RuntimeError, "another_bot"):
                self.run_bot(FakeApi())

    def test_daily_attempt_cap_persists_across_restarts(self):
        api = FakeApi(["wrong", "wrong", "correct"])
        self.run_bot(api, max_candidates=1)
        self.run_bot(api, max_candidates=1)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.report()["stop_reason"], "daily_attempt_limit")

    def test_shortage_and_skip_reasons(self):
        api = FakeApi(["correct", "correct", "correct"])
        api.tasks[0]["block"]["name"] = "table"
        api.tasks[1]["block"]["text"] = '<img src="task.png">'
        api.tasks[2]["has_submissions_restrictions"] = True
        self.assertEqual(self.run_bot(api), 0)
        self.assertEqual(len(self.report()["reasons"]), 3)
        self.assertEqual(self.report()["stop_reason"], "inventory_exhausted")

    def test_disk_error_after_post_can_be_recovered(self):
        api = FakeApi()
        real_save = save_state
        def checkpoint(path, state):
            if Path(path) == self.path and any(x.get("submission_id") for x in state.get("jobs", {}).values()):
                raise OSError("simulated disk failure")
            real_save(path, state)
        with patch("bot.save_state", side_effect=checkpoint):
            with self.assertRaises(OSError):
                self.run_bot(api)
        self.assertNotIn("submission_id", load_state(self.path)["jobs"]["1"])
        self.assertEqual(self.run_bot(api, target=1), 1)
        self.assertEqual(len(api.sent), 1)

    def test_confirm_read_error_preserves_known_submission(self):
        api = FakeApi()
        with patch.object(api, "verdict", side_effect=APIError("stepik", 403)):
            self.run_bot(api)
        self.assertEqual(load_state(self.path)["jobs"]["1"]["phase"], "sending")
        self.assertEqual(self.run_bot(api, target=1), 1)
        self.assertEqual(len(api.sent), 1)

    def test_definite_post_rejection_does_not_block_other_tasks(self):
        api = FakeApi(["correct", "correct"])
        real_submit = api.submit
        def rejected_first(aid, reply):
            if aid == 10:
                raise APIError("stepik", 400)
            return real_submit(aid, reply)
        with patch.object(api, "submit", side_effect=rejected_first):
            self.assertEqual(self.run_bot(api, target=1), 1)
        self.assertEqual(load_state(self.path)["jobs"]["1"]["phase"], "failed")

    def test_missing_server_time_stays_unverified(self):
        api = FakeApi()
        api.server_time = None
        self.assertEqual(self.run_bot(api), 0)
        self.assertEqual(self.report()["stop_reason"], "unresolved_submission")
        self.assertFalse(load_state(self.path)["solved"])

    def test_reconcile_does_not_generate_or_send(self):
        api = FakeApi(["correct", "correct"])
        api.verdict_delay = True
        self.run_bot(api)
        api.verdict_delay = False
        with patch.object(FakeSolver, "solve", side_effect=AssertionError()):
            self.assertEqual(self.run_bot(api, recover_only=True), 1)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.report()["stop_reason"], "reconciled")

    def test_successful_canary_is_not_repeated_for_same_digest(self):
        api = FakeApi(["correct", "correct"])
        self.run_bot(api, canary=True, model_digest="digest")
        self.run_bot(api, canary=True, model_digest="digest")
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.report()["stop_reason"], "canary_already_verified")

    def test_model_preflight_failure_is_in_report(self):
        solver = FakeSolver()
        solver.preflight = Mock(side_effect=ValueError("local_gateway_preflight_failed"))
        api = FakeApi()
        with self.assertRaises(ValueError):
            self.run_bot(api, solver=solver, check_model=True)
        self.assertEqual(self.report()["stop_reason"], "local_gateway_preflight_failed")
        self.assertFalse(api.created)


class ReplyTests(unittest.TestCase):
    def test_numeric_strings_and_invalid_numbers(self):
        for value in ("0.000000000000000001", 3.5, "-1e-10"):
            self.assertEqual(validate_reply(task(kind="number"), {}, {"number": value}), {"number": str(value)})
        for value in (True, None, float("inf"), float("nan"), "NaN", "1,2", "2 kg"):
            with self.assertRaises(ValueError):
                validate_reply(task(kind="number"), {}, {"number": value})

    def test_choice_shuffled_options_and_cardinality(self):
        attempt = {"dataset": {"options": ["B", "A"], "is_multiple_choice": False}}
        self.assertEqual(validate_reply(task(), attempt, {"choices": [True, False]}), {"choices": [True, False]})
        for choices in ([True], [1, 0], [False, False], [True, True]):
            with self.assertRaises(ValueError):
                validate_reply(task(), attempt, {"choices": choices})
        with self.assertRaises(ValueError):
            validate_reply(task(), {}, {"choices": [True, False]})
        attempt["dataset"]["is_multiple_choice"] = True
        self.assertEqual(validate_reply(task(), attempt, {"choices": [True, True]}), {"choices": [True, True]})

    def test_ordering_zero_based_permutation(self):
        for kind, key in (("sorting", "options"), ("matching", "pairs")):
            values = ["A", "B", "C"] if kind == "sorting" else [
                {"first": "A", "second": "C"}, {"first": "B", "second": "A"}, {"first": "C", "second": "B"}]
            attempt = {"dataset": {key: values}}
            self.assertEqual(validate_reply(task(kind=kind), attempt, {"ordering": [2, 0, 1]}), {"ordering": [2, 0, 1]})
            for order in ([0, 0, 2], [1, 2, 3], [True, 0, 2], [0, 1]):
                with self.assertRaises(ValueError):
                    validate_reply(task(kind=kind), attempt, {"ordering": order})

    def test_string_dataset_and_math_formula(self):
        self.assertEqual(validate_reply(task(kind="string"), {"dataset": "dynamic"}, {"text": "42"}), {"text": "42"})
        self.assertEqual(validate_reply(task(kind="math"), {}, {"formula": "x^2"}), {"formula": "x^2"})

    def test_python_exact_language_and_syntax(self):
        step = task(kind="code")
        step["block"]["options"] = {"code_templates": {"python3.12": "# write code", "python": ""}}
        self.assertEqual(validate_reply(step, {}, {"code": "print(42)"})["language"], "python3.12")
        with self.assertRaises(ValueError):
            validate_reply(step, {}, {"code": "def broken("})

    def test_external_or_visual_dataset_skipped(self):
        for attempt in ({"dataset": {"options": ['<img src="q">']}}, {"dataset_url": "https://example.org/data"}):
            with self.assertRaises(ValueError):
                validate_reply(task(), attempt, {"choices": [True]})

    def test_cloud_models_rejected(self):
        for model in ("qwen3:cloud", "https://host/model", "user/model"):
            with self.assertRaises(ValueError):
                ChatModel(model)


class ApiTests(unittest.TestCase):
    class Response:
        def __init__(self, data, status=200):
            self.data, self.status_code, self.headers = data, status, {}
        def json(self):
            return self.data

    def session(self):
        session = Mock()
        session.post.return_value = self.Response({"access_token": "TEST-SECRET", "token_type": "Bearer",
                                                   "scope": "read write", "expires_in": 3600})
        return session

    def test_identity_and_oauth_flow(self):
        session = self.session()
        session.request.return_value = self.Response({"users": [{"id": 42}]})
        api = Stepik("local-id", "local-secret", session)
        self.assertEqual(api.identity(), 42)
        self.assertEqual(session.post.call_args.kwargs["data"], {"grant_type": "client_credentials"})
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_refresh_401_and_retry_429(self):
        session = self.session()
        session.request.side_effect = [self.Response({}, 401), self.Response({}, 429), self.Response({"ok": True})]
        api = Stepik("id", "secret", session)
        with patch("stepik.time.sleep"):
            self.assertEqual(api.get("test"), {"ok": True})
        self.assertEqual(session.post.call_count, 2)

    def test_post_not_retried_and_error_redacted(self):
        import requests
        session = self.session()
        session.request.side_effect = requests.ConnectionError("DO-NOT-LOG-SECRET")
        api = Stepik("id", "secret", session)
        with self.assertRaises(APIError) as error:
            api.submit(1, {"text": "answer"})
        self.assertNotIn("DO-NOT-LOG-SECRET", str(error.exception))
        self.assertEqual(session.request.call_count, 1)

    def test_missing_pagination_fails_closed(self):
        api = Stepik("id", "secret", self.session())
        with patch.object(api, "get", return_value={"submissions": []}):
            with self.assertRaises(APIError):
                list(api.pages("submissions"))

    def test_fetch_many_order_and_completeness(self):
        api = Stepik("id", "secret", self.session())
        with patch.object(api, "request", return_value={"steps": [{"id": 2}, {"id": 1}]}):
            self.assertEqual([x["id"] for x in api.fetch_many("steps", [1, 2])], [1, 2])
            with self.assertRaises(APIError):
                api.fetch_many("steps", [1, 3])

    def test_progress_reference(self):
        api = Stepik("id", "secret", self.session())
        with patch.object(api, "get", return_value={"progresses": [{"is_passed": False}]}) as get:
            self.assertIs(is_solved({"progress": "42_1"}, api), False)
            get.assert_called_once_with("progresses/42_1")

    def test_discovery_exams_and_duplicate_lessons(self):
        api = Stepik("id", "secret", self.session())
        resources = {"sections": [{"id": 10, "is_exam": True, "units": [20]}, {"id": 11, "units": [21, 22]}],
                     "units": [{"id": 21, "lesson": 30}, {"id": 22, "lesson": 30}],
                     "lessons": [{"id": 30, "steps": [40, 41]}],
                     "steps": [{"id": 40, "block": {"name": "choice"}}, {"id": 41, "block": {"name": "text"}}]}
        with patch.object(api, "fetch_many", side_effect=lambda kind, ids: [x for x in resources[kind] if x["id"] in ids]):
            with patch.object(api, "get", return_value={"lessons": [{"steps": [40, 41]}]}) as get:
                self.assertEqual([s["id"] for s, _ in api.steps([{"id": 1, "sections": [10, 11]}])], [40, 41])
                self.assertEqual(get.call_count, 0)
        self.assertEqual(api.notices[0]["reason"], "exam")

    def test_inventory_history_checks_ownership_and_all_pages(self):
        api = Stepik("id", "secret", self.session())
        api.user_id = 42
        pages = [{"submissions": [{"status": "correct", "attempt": 10}], "meta": {"has_next": True}},
                 {"submissions": [{"status": "correct", "attempt": 20}], "meta": {"has_next": False}}]
        with patch.object(api, "get", side_effect=pages), patch.object(api, "fetch_many", side_effect=[[{"id": 10, "user": 42, "step": 5}], [{"id": 20, "user": 42, "step": 6}]]):
            result = api.prepare_inventory()
        self.assertEqual(result["correct_submissions"], 2)
        self.assertTrue(api.correct_history(5))
        self.assertEqual(api.correct_history(7), [])
        api.inventory_correct_steps = None
        with patch.object(api, "get", return_value=pages[1]), patch.object(api, "fetch_many", return_value=[{"id": 20, "user": 99, "step": 6}]):
            with self.assertRaisesRegex(APIError, "inventory_history_owner_mismatch"):
                api.prepare_inventory()
        self.assertIsNone(api.inventory_correct_steps)

    def test_legacy_settled_is_blocked_and_never_treated_as_correct(self):
        api = Stepik("id", "secret", self.session())
        api.user_id = 42
        rows = [{"status": s, "attempt": aid} for s, aid in (("correct", 10), ("settled", 20), ("wrong", 30))]
        attempts = [{"id": aid, "user": 42, "step": aid // 10} for aid in (10, 20, 30)]
        with patch.object(api, "get", return_value={"submissions": rows, "meta": {"has_next": False}}), patch.object(api, "fetch_many", return_value=attempts):
            result = api.prepare_inventory()
        self.assertEqual(result["correct_submissions"], 1)
        self.assertFalse(result["status_filter_exact"])
        self.assertEqual(api.correct_history(1), [{"status": "correct"}])
        self.assertEqual(api.correct_history(2), [{"status": "unknown_history"}])
        self.assertEqual(api.correct_history(3), [])

    def test_untrusted_exception_not_logged(self):
        self.assertEqual(safe_error(ValueError("token=TEST-SECRET")), "ValueError")
        self.assertFalse(full_score({"status": "correct", "score": None}))

    def test_correct_history_and_attempt_lists_follow_all_pages(self):
        api = Stepik("id", "secret", self.session())
        api.user_id = 42
        with patch.object(api, "get", side_effect=[
            {"submissions": [{"id": 1}], "meta": {"has_next": True}},
            {"submissions": [{"id": 2}], "meta": {"has_next": False}},
        ]) as get:
            self.assertEqual([x["id"] for x in api.correct_history(7)], [1, 2])
            self.assertEqual(get.call_args.kwargs["page"], 2)
            self.assertEqual(get.call_args.kwargs["user"], 42)


class ConfigTests(unittest.TestCase):
    def test_private_config_without_shell_eval(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("STEPIK_CLIENT_SECRET=TEST-SECRET\n")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_env(path)
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                load_env(path)
                self.assertEqual(os.environ["STEPIK_CLIENT_SECRET"], "TEST-SECRET")
            path.write_text("STEPIK_CLIENT_SECRET=$(touch_unsafe)\n")
            with self.assertRaises(ValueError):
                load_env(path)


if __name__ == "__main__":
    unittest.main()
