"""Failure-path contracts: no live services and no real credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import run, lesson_reference, review_generation_source
from stepik import APIError
from solver import ChatModel, answer_format
from storage import load_state, save_state
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

    def test_choice_schema_matches_five_live_options_and_preserves_abstention(self):
        step = {"block": {"name": "choice", "text": "Synthetic question"}}
        instruction, fmt = answer_format(step, {"dataset": {"options": list("ABCDE"), "is_multiple_choice": False}})
        self.assertIn("5", instruction)
        self.assertIn("ровно один", instruction)
        schemas = fmt["json_schema"]["schema"]["oneOf"]
        choices = schemas[0]["properties"]["choices"]
        self.assertEqual((choices["minItems"], choices["maxItems"]), (5, 5))
        self.assertEqual(choices["items"], {"type": "boolean"})
        self.assertEqual(schemas[1]["properties"]["skip"], {"const": True})

    def test_invalid_choice_dataset_never_reaches_model(self):
        transport = Transport([])
        with self.assertRaisesRegex(ValueError, "missing_attempt_options"):
            offline_solver(transport, self.temp.name).solve({"block": {"name": "choice"}}, {"dataset": {}})
        self.assertEqual(transport.calls, 0)

    def test_review_blocks_same_rejected_choice_after_option_reordering(self):
        transport = Transport([Response({"answer": '{"explanation":"Synthetic justification","choices":[false,true]}'})])
        step = {"block": {"name": "choice", "text": "Synthetic question"}}
        attempt = {"dataset": {"options": ["B", "A"], "is_multiple_choice": False}}
        with self.assertRaisesRegex(ValueError, "model_repeated_rejected_reply"):
            offline_solver(transport, self.temp.name).solve(step, attempt,
                feedback={"status": "wrong", "selected_options": ["A"]})

    def test_review_passes_verified_feedback_and_requests_explanation(self):
        class Reviewed(Transport):
            def post(inner, url, **kwargs):
                payload = kwargs["json"]
                prompt = json.loads(payload["messages"][1]["content"])
                self.assertEqual(prompt["previous_wrong"], {"status": "wrong", "rejected_choices": [[False, True]]})
                schema = payload["response_format"]["json_schema"]["schema"]["oneOf"][0]
                self.assertEqual(schema["required"], ["choices", "explanation"])
                return super().post(url, **kwargs)
        transport = Reviewed([Response({"answer": '{"explanation":"Synthetic justification","choices":[true,false]}'})])
        reply = offline_solver(transport, self.temp.name).solve(
            {"block": {"name": "choice", "text": "Synthetic question"}},
            {"dataset": {"options": ["A", "B"], "is_multiple_choice": False}},
            feedback={"status": "wrong", "selected_options": ["B"]})
        self.assertEqual(reply, {"choices": [True, False]})
        self.assertNotIn("explanation", reply)


class GroundingTests(unittest.TestCase):
    setUp = GatewayTests.setUp
    def test_lesson_reference_retains_types_when_classification_says_both(self):
        class Course:
            def get(inner, path):
                return {"lessons": [{"id": 5, "steps": [1, 3]}]}
            def fetch_many(inner, resource, ids):
                return [{"id": 1, "lesson": 5, "block": {"name": "text", "text":
                    "<h2>Типы</h2><p>Адаптационное тестирование (adaptation) – проверка адаптации.</p>"
                    "<p>Оба типа относятся к нефункциональному тестированию.</p>"}}]
        refs = lesson_reference(Course(), {"id": 3, "lesson": 5, "block": {
            "text": "Какие типы являются нефункциональными?"}}, ["Адаптационное тестирование"])
        self.assertIn("Адаптационное тестирование", refs[0]["text"])
        self.assertIn("нефункциональному", refs[0]["text"])
        self.assertNotIn("Оба", refs[0]["text"])
    def test_reference_packing_skips_oversize_sentences_and_tracks_sources(self):
        class Grounded(Transport):
            def post(inner, url, **kwargs):
                payload = kwargs["json"]
                data = json.loads(payload["messages"][1]["content"])
                self.assertEqual(data["reference"], [{"source_step": 12, "text": "A short complete synthetic fact."}])
                self.assertLessEqual(sum(len(x["content"].encode()) for x in payload["messages"]), 1504)
                return super().post(url, **kwargs)
        transport = Grounded([Response({"answer": '{"explanation":"Synthetic justification","choices":[true,false]}'})])
        model = offline_solver(transport, self.temp.name)
        model.solve({"block": {"name": "choice", "text": "Synthetic question"}},
                    {"dataset": {"options": ["A", "B"], "is_multiple_choice": False}},
                    feedback={"status": "wrong", "selected_options": ["B"], "reference": [
                        {"source_step": 11, "text": "x" * 4000},
                        {"source_step": 12, "text": "A short complete synthetic fact."}]})
        self.assertEqual(model.metrics["reference_source_steps"], [12])

    def test_review_blocks_any_rejected_selection(self):
        transport = Transport([Response({"answer": '{"explanation":"Synthetic justification","choices":[true,false]}'})])
        with self.assertRaisesRegex(ValueError, "model_repeated_rejected_reply"):
            offline_solver(transport, self.temp.name).solve(
                {"block": {"name": "choice", "text": "Synthetic question"}},
                {"dataset": {"options": ["A", "B"], "is_multiple_choice": False}},
                feedback={"status": "wrong", "selected_options": ["B"], "rejected_selections": [["A"], ["B"]]})

    def test_lesson_reference_reads_only_earlier_theory(self):
        class TheoryApi:
            def get(inner, path):
                return {"lessons": [{"id": 5, "steps": [1, 2, 3, 4]}]}
            def fetch_many(inner, resource, ids):
                self.assertEqual((resource, ids), ("steps", [1, 2]))
                return [{"id": 1, "lesson": 5, "block": {"name": "text", "text": "<h2>Надёжность</h2><p>Надёжность проверяют испытаниями.</p>"}},
                        {"id": 2, "lesson": 5, "block": {"name": "choice", "text": "Private graded question"}}]
        refs = lesson_reference(TheoryApi(), {"id": 3, "lesson": 5, "block": {"text": "Проверяем надёжность"}})
        self.assertTrue(refs)
        self.assertEqual({x["source_step"] for x in refs}, {1})
        self.assertNotIn("Private graded question", json.dumps(refs))

    def test_lesson_reference_rejects_wrong_lesson_identity(self):
        class WrongLesson:
            def get(inner, path):
                return {"lessons": [{"id": 99, "steps": [3]}]}
        with self.assertRaisesRegex(ValueError, "lesson_context_identity_mismatch"):
            lesson_reference(WrongLesson(), {"id": 3, "lesson": 5, "block": {"text": "Question"}})


class RetryApi(FakeApi):
    def __init__(self):
        super().__init__(["wrong"])
        self.submissions = []
    def existing_attempt(self, aid, sid):
        return {"id": aid, "step": sid, "user": 42,
                "dataset": {"options": ["A", "B"], "is_multiple_choice": False}}
    def submit(self, aid, reply):
        self.sent.append((aid, reply))
        sid = aid // 10
        row = {"id": 1001 + len(self.submissions), "attempt": aid, "reply": reply,
               "status": self.statuses[sid - 1], "score": "1" if self.statuses[sid - 1] == "correct" else "0",
               "time": self.server_time}
        self.submissions.append(row)
        if row["status"] == "correct":
            self.solved.add(sid)
        return row["id"]
    def verdict(self, subid):
        return next(x for x in self.submissions if x["id"] == subid)
    def attempt_submissions(self, aid):
        return [x for x in self.submissions if x["attempt"] == aid]


class TrialTests(unittest.TestCase):
    setUp = BotTests.setUp
    run_bot = BotTests.run_bot
    report = BotTests.report
    def test_review_resume_reuses_never_submitted_attempt_and_preserves_history(self):
        api = RetryApi()
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1})
        class Abstain:
            def solve(inner, step, attempt, feedback=None):
                raise ValueError("model_abstained_or_invalid_json")
        self.run_bot(api, solver=Abstain(), canary=True, course_ids=[5], candidate_step_ids={1}, retry_wrong=True)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.created, [1, 1])
        class Review:
            def solve(inner, step, attempt, feedback=None):
                return {"choices": [False, True]}
        api.statuses = ["correct"]
        self.assertEqual(self.run_bot(api, solver=Review(), canary=True, course_ids=[5],
            candidate_step_ids={1}, retry_wrong=True, resume_review=True), 1)
        self.assertEqual(api.created, [1, 1])
        self.assertEqual(len(api.sent), 2)
        state = load_state(self.path)
        self.assertEqual(len(state["job_history"]["1"]), 1)
        self.assertEqual(state["jobs"]["1"]["review_retries"], 1)

    def test_review_resume_blocks_possible_writes_and_exhausted_budget(self):
        import copy
        prior = {"submission_id": 1001}
        job = {"phase": "failed", "reason": "model_abstained_or_invalid_json", "canary": True,
               "course": 5, "attempt_id": 10, "baseline": [], "retry_of": 1001}
        state = {"jobs": {"1": job}, "job_history": {"1": [prior]}}
        self.assertEqual(review_generation_source(state, "1", 5), prior)
        for change in ({"sent_at": "unknown"}, {"submission_id": 1002}, {"reply": {}},
                       {"phase": "sending"}, {"review_retries": 2}, {"retry_of": 999}, {"course": 99}):
            altered = copy.deepcopy(state)
            altered["jobs"]["1"].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "review_generation_retry_not_safe"):
                review_generation_source(altered, "1", 5)

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

    def test_canary_can_only_send_audited_step_in_selected_course(self):
        api = FakeApi(["correct", "correct"])
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={2})
        self.assertEqual(api.created, [2])
        self.assertEqual([a for a, _ in api.sent], [20])

    def prepare_rejected_generation(self):
        api = FakeApi()
        class InvalidSolver:
            def solve(self, step, attempt):
                return {"choices": [True]}
        self.run_bot(api, solver=InvalidSolver(), canary=True, max_candidates=1,
                     course_ids=[5], candidate_step_ids={1})
        self.assertEqual(api.created, [1])
        self.assertEqual(api.sent, [])
        self.assertEqual(load_state(self.path)["jobs"]["1"]["reason"], "invalid_choices")
        def existing(aid, sid):
            self.assertEqual((aid, sid), (10, 1))
            return {"id": aid, "dataset": {"options": ["A", "B"], "is_multiple_choice": False}}
        api.existing_attempt = existing
        return api

    def test_explicit_generation_retry_reuses_attempt_and_sends_only_once(self):
        api = self.prepare_rejected_generation()
        result = self.run_bot(api, canary=True, max_candidates=1, course_ids=[5],
                              candidate_step_ids={1}, resume_generation=True)
        self.assertEqual(result, 1)
        self.assertEqual(api.created, [1])
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(load_state(self.path)["jobs"]["1"]["generation_retries"], 1)
        with self.assertRaisesRegex(ValueError, "generation_retry_not_safe"):
            self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, resume_generation=True)
        self.assertEqual(len(api.sent), 1)

    def test_retry_rejects_any_possible_post_and_is_bounded(self):
        api = self.prepare_rejected_generation()
        base = load_state(self.path)
        import copy
        changes = [{"sent_at": "unknown"}, {"submission_id": 11}, {"reply": {}},
                   {"generation_retries": 1}, {"baseline": [11]}, {"reason": "wrong"},
                   {"attempt_id": None}, {"course": 99}]
        for change in changes:
            state = copy.deepcopy(base)
            state["jobs"]["1"].update(change)
            save_state(self.path, state)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "generation_retry_not_safe"):
                self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, resume_generation=True)
        self.assertEqual(api.sent, [])
        self.assertEqual(api.created, [1])

    def test_retry_aborts_when_server_already_has_submission(self):
        api = self.prepare_rejected_generation()
        api.attempt_submissions = lambda aid: [{"id": 11}]
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, resume_generation=True)
        self.assertEqual(api.sent, [])
        self.assertEqual(self.report()["reasons"], {"generation_retry_submission_exists": 1})

    def test_retry_requires_one_explicit_course_and_step(self):
        for kwargs in ({}, {"canary": True}, {"canary": True, "course_ids": [5], "candidate_step_ids": {1, 2}}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "exact_canary_scope"):
                self.run_bot(FakeApi(), resume_generation=True, **kwargs)

    def test_explicit_known_wrong_retry_preserves_history_and_counts_one_success(self):
        api = RetryApi()
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, max_candidates=1)
        self.assertEqual(len(api.sent), 1)
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1})
        self.assertEqual(len(api.sent), 1)  # ordinary rerun still cannot bypass attempted_today
        class Review:
            def solve(inner, step, attempt, feedback=None):
                self.assertEqual(feedback, {"status": "wrong", "selected_options": ["A"]})
                return {"choices": [False, True]}
        api.statuses = ["correct"]
        result = self.run_bot(api, solver=Review(), canary=True, course_ids=[5],
                              candidate_step_ids={1}, max_candidates=1, retry_wrong=True)
        state = load_state(self.path)
        self.assertEqual(result, 1)
        self.assertEqual(len(api.sent), 2)
        self.assertEqual(state["job_history"]["1"][0]["submission_id"], 1001)
        self.assertEqual(state["jobs"]["1"]["submission_id"], 1002)
        self.assertEqual(state["jobs"]["1"]["retry_of"], 1001)
        self.assertEqual(state["jobs"]["1"]["wrong_retries"], 1)

    def test_known_wrong_retry_stops_if_live_verdict_or_history_changes(self):
        api = RetryApi()
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1})
        api.submissions[0]["status"] = "evaluation"
        with self.assertRaisesRegex(ValueError, "wrong_retry_verdict_changed"):
            self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, retry_wrong=True)
        api.submissions[0]["status"] = "wrong"
        api.submissions.append({"id": 1002, "attempt": 10, "status": "evaluation"})
        with self.assertRaisesRegex(ValueError, "wrong_retry_history_changed"):
            self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, retry_wrong=True)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.created, [1])

    def test_known_wrong_retry_rejects_exhausted_budget_and_nonterminal_job(self):
        api = RetryApi()
        self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1})
        original = load_state(self.path)
        import copy
        for change in ({"phase": "sending"}, {"wrong_retries": 3}, {"course": 99}, {"reason": "accepted"}):
            state = copy.deepcopy(original)
            state["jobs"]["1"].update(change)
            # For 'sending', recover may establish a terminal wrong; the other
            # guards are still verified after recovery, never by erasing state.
            if change.get("phase") == "sending":
                state["jobs"]["1"]["submission_id"] = 9999
            save_state(self.path, state)
            if change.get("phase") == "sending":
                with patch.object(api, "verdict", side_effect=APIError("verdict_timeout")):
                    self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, retry_wrong=True)
            else:
                with self.assertRaisesRegex(ValueError, "wrong_retry_not_safe"):
                    self.run_bot(api, canary=True, course_ids=[5], candidate_step_ids={1}, retry_wrong=True)
        self.assertEqual(len(api.sent), 1)

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
