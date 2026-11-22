"""Failure-path contracts: no live services and no real credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import run
from solver import ChatModel
from storage import load_state
from test_bot import BotTests, FakeApi, FakeSolver
from test_integration import Response, offline_solver


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
    def post(self, url, **kwargs):
        self.calls += 1
        assert url == "http://127.0.0.1:18085/v1/chat/completions"
        assert kwargs["allow_redirects"] is False
        return next(self.responses)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.step = {"block": {"name": "number", "text": "17*23"}}

    def test_busy_then_streaming_success(self):
        busy = Response({}); busy.status_code = 429; busy.headers = {"Retry-After": "10"}
        transport = Transport([busy, Response({"answer": '{"number":"391"}'})])
        solver = offline_solver(transport, self.temp.name)
        with patch("solver.time.sleep") as sleep:
            self.assertEqual(solver.solve(self.step, {}), {"number": "391"})
            sleep.assert_called_once_with(10)
        self.assertEqual(solver.metrics["busy_retries"], 1)
        self.assertEqual(solver.metrics["usage"]["completion_tokens"], 12)
        self.assertFalse(solver.session.trust_env)

    def test_busy_budget_stops(self):
        busy = Response({}); busy.status_code = 429; busy.headers = {"Retry-After": "30"}
        transport = Transport([busy] * 5)
        solver = offline_solver(transport, self.temp.name)
        solver.busy_seconds = 30
        with patch("solver.time.sleep"), self.assertRaisesRegex(ValueError, "model_busy_retry_budget"):
            solver.solve(self.step, {})
        self.assertEqual(transport.calls, 2)

    def test_auth_failure_is_not_retried(self):
        denied = Response({}); denied.status_code = 401
        transport = Transport([denied])
        with self.assertRaisesRegex(ValueError, "model_http_401"):
            offline_solver(transport, self.temp.name).solve(self.step, {})
        self.assertEqual(transport.calls, 1)

    def test_truncated_and_disconnected_streams_rejected(self):
        for reason, done in (("length", True), ("stop", False)):
            class Incomplete(Response):
                def iter_lines(self, **kwargs):
                    yield 'data: ' + json.dumps({"choices": [{"delta": {"content": '{"number":"391"}'}, "finish_reason": reason}]})
                    if done:
                        yield 'data: [DONE]'
            with self.subTest(reason=reason, done=done), self.assertRaisesRegex(ValueError, "truncated_or_incomplete"):
                offline_solver(Transport([Incomplete({})]), self.temp.name).solve(self.step, {})

    def test_long_input_never_reaches_model(self):
        transport = Transport([])
        self.step["block"]["text"] *= 1000
        with self.assertRaisesRegex(ValueError, "context_too_large"):
            offline_solver(transport, self.temp.name).solve(self.step, {})
        self.assertEqual(transport.calls, 0)

    def test_key_permissions_and_remote_endpoint_rejected(self):
        solver = offline_solver(Transport([]), self.temp.name)
        solver.key_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "key_file_must_be_private"):
            solver.headers()
        with self.assertRaisesRegex(ValueError, "authenticated_local_gateway"):
            ChatModel(base_url="https://skin-cancer-ai.ru/v1")


class TrialTests(unittest.TestCase):
    setUp = BotTests.setUp
    run_bot = BotTests.run_bot
    report = BotTests.report
    def test_trial_requires_canary_and_stops_after_three_posts(self):
        api = FakeApi(["correct"] * 6)
        with self.assertRaisesRegex(RuntimeError, "verified_canary_required"):
            self.run_bot(api, trial=True, model_digest="same")
        self.assertEqual(api.sent, [])
        self.run_bot(api, canary=True, model_digest="same")
        self.run_bot(api, trial=True, model_digest="same")
        state = load_state(self.path)
        self.assertEqual(len(api.sent), 4)
        self.assertEqual(len(state["trial"]["steps"]), 3)
        with self.assertRaisesRegex(RuntimeError, "user_canary_verification_required"):
            self.run_bot(api, require_trial=True, model_digest="same")

    def test_wrong_trial_answers_do_not_unlock_daily_mode(self):
        api = FakeApi(["correct", "wrong", "correct", "wrong", "correct"])
        self.run_bot(api, canary=True, model_digest="same")
        self.run_bot(api, trial=True, model_digest="same")
        self.assertEqual(len(load_state(self.path)["trial"]["steps"]), 1)
        with self.assertRaisesRegex(RuntimeError, "three_verified_trial"):
            self.run_bot(api, require_trial=True, model_digest="same")

    def test_wrapped_get_reply_recovers_without_duplicate_post(self):
        api = FakeApi()
        api.timeout_after_write = True
        self.run_bot(api)
        api.rows[10]["reply"] = {"reply": api.rows[10]["reply"]}
        self.assertEqual(self.run_bot(api, recover_only=True), 1)
        self.assertEqual(len(api.sent), 1)

    def test_day_boundary_after_progress_requests_prevents_post(self):
        api = FakeApi()
        with patch("bot.can_start", side_effect=[True, True, False]):
            self.run_bot(api)
        self.assertEqual(api.created, [1])
        self.assertEqual(api.sent, [])

    def test_interrupted_inventory_is_never_marked_complete(self):
        api = FakeApi()
        def interrupted(courses):
            yield api.tasks[0], courses[0]
            raise KeyboardInterrupt()
        api.steps = interrupted
        with self.assertRaises(KeyboardInterrupt):
            run(api, FakeSolver(), self.path, dry_run=True, expected_user=42)
        report = self.report(dry=True)
        self.assertFalse(report["inventory_complete"])
        self.assertEqual(report["stop_reason"], "interrupted")


del BotTests


if __name__ == "__main__":
    unittest.main()
