"""Offline wire-contract tests: real Stepik client, solver and durable runner together.

Fixtures follow Stepik's public Android model definitions, not account captures.
No sockets, credentials or generated code execution are used.
"""
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot import run
from solver import ChatModel
from stepik import Stepik
from storage import load_state

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
KINDS = ["choice", "number", "string", "sorting", "matching", "math", "code"]
ANSWERS = {"choice": {"choices": [False, True]}, "number": {"number": "391"},
           "string": {"text": "hello"}, "sorting": {"ordering": [1, 0]},
           "matching": {"ordering": [1, 0]}, "math": {"formula": "x^2"}, "code": {"code": "print(42)"}}


class Response:
    status_code = 200
    headers = {}
    def __init__(self, data):
        self.data = data
    def json(self):
        return copy.deepcopy(self.data)
    def raise_for_status(self):
        pass
    def close(self):
        pass
    def iter_lines(self, **kwargs):
        yield 'data: ' + json.dumps({"choices": [{"delta": {"content": self.data["answer"]}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 100, "completion_tokens": 12}})
        yield 'data: [DONE]'


class StepikTransport:
    def __init__(self):
        self.sent, self.calls = {}, []
        self.tasks = [{"id": i + 1, "block": {"name": kind, "text": "Question"},
                       "progress": f"42_s{i + 1}"} for i, kind in enumerate(KINDS)]
        self.tasks[-1]["block"]["options"] = {"code_templates": {"python3.12": "# code"}}

    def post(self, url, **kwargs):
        assert url == "https://stepik.org/oauth2/token/"
        assert kwargs["data"] == {"grant_type": "client_credentials"}
        assert kwargs["allow_redirects"] is False
        return Response({"access_token": "OFFLINE-TOKEN-SECRET", "token_type": "Bearer",
                         "scope": "read write", "expires_in": 3600})

    def request(self, method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer OFFLINE-TOKEN-SECRET"
        assert kwargs["allow_redirects"] is False
        path = url.removeprefix("https://stepik.org/api/")
        self.calls.append((method, path))
        params = dict(kwargs.get("params", {}))
        if method == "POST":
            if path == "attempts":
                sid = kwargs["json"]["attempt"]["step"]
                dataset = {"choice": {"options": ["B", "A"], "is_multiple_choice": False},
                           "sorting": {"options": ["20", "10"]},
                           "matching": {"pairs": [{"first": "one", "second": "два"}, {"first": "two", "second": "один"}]}}
                return Response({"attempts": [{"id": sid * 10, "step": sid, "user": 42,
                                               "dataset": dataset.get(KINDS[sid - 1], "dynamic condition")}]})
            if path == "submissions":
                body = kwargs["json"]["submission"]
                aid, reply = body["attempt"], body["reply"]
                kind = KINDS[aid // 10 - 1]
                expected = dict(ANSWERS[kind])
                if kind == "code":
                    expected["language"] = "python3.12"
                assert reply == expected, (kind, reply)
                sub = {"id": aid + 1, "attempt": aid, "reply": reply, "status": "correct",
                       "score": "1.0", "time": NOW.isoformat()}
                self.sent[aid] = sub
                return Response({"submissions": [sub]})
            raise AssertionError(path)
        if path == "stepics/1":
            return Response({"stepics": [{"id": 1}], "users": [{"id": 42}]})
        if path.startswith("submissions/"):
            return Response({"submissions": [self.sent[int(path.split("/")[1]) - 1]]})
        if path.startswith("progresses/"):
            sid = int(path.split("_s")[1])
            return Response({"progresses": [{"id": path.split("/")[1], "is_passed": sid * 10 in self.sent}]})
        if path.startswith("steps/"):
            return Response({"steps": [self.tasks[int(path.split("/")[1]) - 1]]})
        if path == "submissions":
            if "attempt" in params:
                rows = [self.sent[params["attempt"]]] if params["attempt"] in self.sent else []
            else:
                assert params["user"] == 42 and params.get("status") in (None, "correct")
                rows = ([self.sent[params["step"] * 10]] if params["step"] * 10 in self.sent else []) if "step" in params else list(self.sent.values())
            return Response({"submissions": rows, "meta": {"has_next": False}})
        if path == "attempts":
            return Response({"attempts": [], "meta": {"has_next": False}})
        if path == "progresses":
            ids = [value for key, value in kwargs["params"] if key == "ids[]"]
            return Response({"progresses": [{"id": pid, "is_passed": int(pid.split("_s")[1]) * 10 in self.sent} for pid in ids], "meta": {"has_next": False}})
        resources = {
            "user-courses": [{"id": 100, "course": 5}], "courses": [{"id": 5, "sections": [10]}],
            "sections": [{"id": 10, "units": [20], "is_exam": False}],
            "units": [{"id": 20, "lesson": 30, "is_active": True}],
            "lessons/30": [{"id": 30, "steps": list(range(1, 8))}], "lessons": [{"id": 30, "steps": list(range(1, 8))}], "steps": self.tasks,
        }
        return Response({path.split("/")[0]: resources[path], "meta": {"has_next": False}})


class ChatTransport:
    def __init__(self):
        self.generations = 0
    def get(self, url, **kwargs):
        assert kwargs["headers"]["Authorization"].startswith("Bearer llm_")
        if url.endswith("/llm/props"):
            return Response({"default_generation_settings": {"n_ctx": 2048}})
        assert url == "http://127.0.0.1:18085/v1/models"
        return Response({"data": [{"id": "raspberry-llm", "meta": {"n_ctx": 2048}}]})
    def post(self, url, **kwargs):
        assert url == "http://127.0.0.1:18085/v1/chat/completions"
        assert kwargs["headers"]["Authorization"].startswith("Bearer llm_")
        data = kwargs["json"]
        assert data["stream"] is True and data["response_format"] == {"type": "json_object"}
        assert data["max_tokens"] == 384 and data["model"] == "raspberry-llm"
        block = json.loads(data["messages"][1]["content"])["block"]
        self.generations += 1
        return Response({"answer": json.dumps(ANSWERS[block["name"]])})


def offline_solver(transport, folder):
    key = Path(folder) / "llm.key"
    key.write_text("llm_" + "OFFLINE-KEY-" * 4)
    key.chmod(0o600)
    return ChatModel(session=transport, key_file=key, identity=lambda: "offline-weights-sha256")


class WireTests(unittest.TestCase):
    def test_all_supported_wire_formats_and_idempotent_restart(self):
        remote, local = StepikTransport(), ChatTransport()
        api = Stepik("offline-id", "OFFLINE-CLIENT-SECRET", session=remote)
        with tempfile.TemporaryDirectory() as folder, patch("bot.utcnow", return_value=NOW), patch("sys.stdout", new_callable=io.StringIO) as output:
            solver = offline_solver(local, folder)
            path = Path(folder) / "progress.json"
            self.assertEqual(run(api, solver, path, target=7, dry_run=False, expected_user=42, check_model=True), 7)
            state = load_state(path)
            self.assertEqual(len(state["solved"]), 7)
            self.assertEqual(local.generations, 7)
            self.assertEqual(run(api, solver, path, target=7, dry_run=False, expected_user=42, check_model=True), 7)
            self.assertEqual(len(remote.sent), 7)
            report = (path.parent / "reports/latest.json").read_text()
            for text in (report, path.read_text(), output.getvalue()):
                self.assertNotIn("OFFLINE-CLIENT-SECRET", text)
                self.assertNotIn("OFFLINE-TOKEN-SECRET", text)

    def test_inventory_calls_no_attempts_submissions_or_model(self):
        remote, local = StepikTransport(), ChatTransport()
        with tempfile.TemporaryDirectory() as folder, patch("bot.utcnow", return_value=NOW), patch("sys.stdout", new_callable=io.StringIO):
            path = Path(folder) / "progress.json"
            run(Stepik("id", "secret", session=remote), offline_solver(local, folder), path)
            self.assertTrue(all(method == "GET" for method, _ in remote.calls))
            self.assertEqual(local.generations, 0)
            self.assertFalse(path.exists())
            self.assertEqual(json.loads((path.parent / "reports/latest-dry-run.json").read_text())["candidates"], 7)
            report = json.loads((path.parent / "reports/latest-dry-run.json").read_text())
            self.assertTrue(report["inventory_complete"])
            self.assertEqual(len(report["api_samples"]), 7)


if __name__ == "__main__":
    unittest.main()
