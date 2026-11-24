"""Stepik API. Never include response bodies, credentials or URLs in errors."""
import time
from datetime import datetime, timezone

import requests

BASE = "https://stepik.org"
TERMINAL = {"correct", "wrong", "partially_correct"}


class APIError(RuntimeError):
    def __init__(self, operation, status=None):
        self.status = status
        super().__init__(operation + (f"_http_{status}" if status else "_unavailable"))


def one(payload, key):
    values = payload.get(key)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise APIError("invalid_" + key)
    return values[0]


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("missing_server_time")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("ambiguous_server_time")
    return result.astimezone(timezone.utc)


class Stepik:
    def __init__(self, client_id, client_secret, session=None):
        self.client_id, self.client_secret = client_id, client_secret
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.token, self.expires_at = None, 0
        self.user_id = None
        self.notices = []
        self.read_progress = None
        self.inventory_correct_steps = None
        self.inventory_uncertain_steps = set()
        self.read_only = False

    def prepare_inventory(self):
        """Build a read-only index of the complete own correct history.

        Submissions refer to attempts, so ownership and step IDs are established
        through the real attempt objects, never inferred from submission IDs.
        This cache exists only in this audit process. Write runs use fresh GETs.
        """
        if self.user_id is None:
            raise APIError("identity_required")
        found, uncertain, count, statuses = set(), set(), 0, {}
        for page in range(1, 10001):
            response = self.get("submissions", user=self.user_id, status="correct", page=page)
            rows, meta = response.get("submissions"), response.get("meta", {})
            if not isinstance(rows, list) or type(meta.get("has_next")) is not bool:
                raise APIError("inventory_history_schema")
            if any(not isinstance(r.get("status"), str) or type(r.get("attempt")) is not int for r in rows):
                raise APIError("inventory_history_schema")
            attempts = self.fetch_many("attempts", [r["attempt"] for r in rows])
            if any(a.get("user") != self.user_id or type(a.get("step")) is not int for a in attempts):
                raise APIError("inventory_history_owner_mismatch")
            by_id = {a["id"]: a for a in attempts}
            for row in rows:
                status, sid = row["status"], by_id[row["attempt"]]["step"]
                statuses[status] = statuses.get(status, 0) + 1
                if status == "correct":
                    found.add(sid)
                elif status not in {"wrong", "partially_correct"}:
                    uncertain.add(sid)
            count += len(rows)
            if self.read_progress:
                self.read_progress("history_page_" + str(page))
            if not meta["has_next"]:
                self.inventory_correct_steps = found
                self.inventory_uncertain_steps = uncertain
                return {"returned_submissions": count, "correct_submissions": statuses.get("correct", 0),
                        "unique_previously_correct_steps": len(found), "unknown_or_pending_steps": len(uncertain),
                        "status_counts": statuses, "history_pages": page,
                        "status_filter_exact": set(statuses) <= {"correct"}}
        raise APIError("pagination_limit")

    def auth(self):
        if self.token and time.monotonic() < self.expires_at - 60:
            return
        try:
            res = self.session.post(
                BASE + "/oauth2/token/", data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret), timeout=(10, 30),
                allow_redirects=False,
            )
            if res.status_code != 200:
                raise APIError("oauth", res.status_code)
            data = res.json()
            if not isinstance(data, dict):
                raise APIError("oauth_schema")
            if not isinstance(data.get("access_token"), str) or not data["access_token"]:
                raise APIError("oauth_schema")
            if str(data.get("token_type", "")).lower() != "bearer":
                raise APIError("oauth_token_type")
            scopes = set(data.get("scope", "").split())
            if not {"read", "write"} <= scopes:
                raise APIError("oauth_scopes")
            self.token = data["access_token"]
            self.expires_at = time.monotonic() + int(data["expires_in"])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise APIError("oauth") from None

    def request(self, method, path, **kwargs):
        if self.read_only and method != "GET":
            raise ValueError("read_only_stepik_client_cannot_write")
        refreshed = False
        for retry in range(4):
            self.auth()
            try:
                res = self.session.request(
                    method, BASE + "/api/" + path,
                    headers={"Authorization": "Bearer " + self.token},
                    timeout=(10, 45), allow_redirects=False, **kwargs,
                )
            except requests.RequestException:
                if method == "GET" and retry < 3:
                    time.sleep(2 ** retry)
                    continue
                raise APIError("stepik_transport") from None
            if res.status_code == 401 and not refreshed:
                self.token = None
                refreshed = True
                continue
            if method == "GET" and (res.status_code == 429 or res.status_code >= 500) and retry < 3:
                try:
                    delay = min(30, max(1, int(res.headers.get("Retry-After", 2 ** retry))))
                except (ValueError, TypeError):
                    delay = 2 ** retry
                time.sleep(delay)
                continue
            if not 200 <= res.status_code < 300:
                raise APIError("stepik", res.status_code)
            try:
                data = res.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (ValueError, TypeError):
                raise APIError("stepik_schema") from None
        raise APIError("stepik_retry_limit")

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def pages(self, kind, **filters):
        for page in range(1, 10001):
            response = self.get(kind, page=page, **filters)
            values, meta = response.get(kind), response.get("meta", {})
            if not isinstance(values, list) or type(meta.get("has_next")) is not bool:
                raise APIError("pagination_schema")
            yield from values
            if not meta["has_next"]:
                return
        raise APIError("pagination_limit")

    def fetch_many(self, kind, ids):
        result = {}
        ids = list(dict.fromkeys(ids))
        # Verified against the live API: read-only attempt lookups return all
        # 100 requested IDs. Other resources and write clients keep small batches.
        size = 100 if self.read_only and kind == "attempts" else 20
        for start in range(0, len(ids), size):
            group = ids[start:start + size]
            for page in range(1, 10001):
                response = self.request("GET", kind, params=[("ids[]", x) for x in group] + [("page", page)])
                values = response.get(kind)
                if not isinstance(values, list):
                    raise APIError("collection_schema")
                result.update({x["id"]: x for x in values})
                if not response.get("meta", {}).get("has_next"):
                    break
            else:
                raise APIError("pagination_limit")
            if any(x not in result for x in group):
                raise APIError("incomplete_" + kind)
        return [result[x] for x in ids]

    def identity(self):
        data = self.get("stepics/1")
        profile = one(data, "users" if data.get("users") else "profiles")
        user = profile.get("id")
        if type(user) is not int or user <= 0:
            raise APIError("oauth_identity")
        self.user_id = user
        return user

    def courses(self):
        # Same endpoint as Stepik's My Courses; includes archived courses.
        refs = list(self.pages("user-courses"))
        return self.fetch_many("courses", [x["course"] for x in refs])

    def notice(self, reason, **ids):
        self.notices.append(dict(reason=reason, **ids))

    def steps(self, courses):
        seen_lessons, seen_steps = set(), set()
        for course in courses:
            cid = course["id"]
            if self.read_progress:
                self.read_progress(cid)
            try:
                for section in self.fetch_many("sections", course.get("sections", [])):
                    if section.get("is_exam") or section.get("has_proctor_session"):
                        self.notice("exam", course=cid, section=section["id"])
                        continue
                    if unavailable(section):
                        self.notice("section_unavailable", course=cid, section=section["id"])
                        continue
                    lesson_ids = []
                    for unit in self.fetch_many("units", section.get("units", [])):
                        if unavailable(unit):
                            self.notice("unit_unavailable", course=cid, unit=unit["id"])
                            continue
                        lesson_id = unit.get("lesson")
                        if lesson_id and lesson_id not in seen_lessons:
                            lesson_ids.append(lesson_id)
                    lessons = self.fetch_many("lessons", lesson_ids)
                    steps = self.fetch_many("steps", [sid for lesson in lessons for sid in lesson.get("steps", []) if sid not in seen_steps])
                    # Live API verified: ids[] works for lessons, steps and progress.
                    pids = [s["progress"] for s in steps if isinstance(s.get("progress"), str)
                            and s.get("block", {}).get("name") in {"choice", "string", "number", "code", "sorting", "matching", "math"}]
                    if pids:
                        progress = {p["id"]: p for p in self.fetch_many("progresses", pids)}
                        steps = [dict(s, progress=progress[s["progress"]]) if isinstance(s.get("progress"), str) and s["progress"] in progress else s for s in steps]
                    seen_lessons.update(lesson_ids)
                    for step in steps:
                        if step["id"] not in seen_steps:
                            seen_steps.add(step["id"])
                            yield step, course
            except APIError as exc:
                if exc.status == 401:
                    raise
                self.notice(str(exc), course=cid)

    def step(self, step_id):
        step = one(self.get(f"steps/{step_id}"), "steps")
        if step.get("id") != step_id:
            raise APIError("step_identity")
        return step

    def verify_submission_owner(self, attempt_id, step_id):
        expected = self.user_id
        if expected is None or self.identity() != expected:
            raise APIError("oauth_identity_changed")
        attempt = one(self.get(f"attempts/{attempt_id}"), "attempts")
        if attempt.get("id") != attempt_id or attempt.get("user") != expected or attempt.get("step") != step_id:
            raise APIError("accepted_attempt_owner_mismatch")

    def correct_history(self, step_id):
        if self.user_id is None:
            raise APIError("identity_required")
        if self.inventory_correct_steps is not None:
            if step_id in self.inventory_correct_steps:
                return [{"status": "correct"}]
            return [{"status": "unknown_history"}] if step_id in self.inventory_uncertain_steps else []
        # Live account evidence: status=correct also returns legacy settled rows.
        # Do not label them correct; unknown/pending history blocks a fresh answer.
        return [r for r in self.pages("submissions", step=step_id, user=self.user_id, status="correct", order="asc")
                if r.get("status") not in {"wrong", "partially_correct"}]

    def create_attempt(self, step_id):
        attempt = one(self.request("POST", "attempts", json={"attempt": {"step": step_id}}), "attempts")
        if attempt.get("step") != step_id or attempt.get("user") != self.user_id:
            raise APIError("attempt_identity")
        if type(attempt.get("id")) is not int or attempt["id"] <= 0:
            raise APIError("attempt_id")
        return attempt

    def submit(self, attempt_id, reply):
        data = self.request("POST", "submissions", json={"submission": {"attempt": attempt_id, "reply": reply}})
        return one(data, "submissions")["id"]

    def attempt_submissions(self, attempt_id):
        return list(self.pages("submissions", attempt=attempt_id, order="desc"))

    def audit_step(self, step_id):
        """Read existing objects only. Persist field names/types, never reply values."""
        result = {"step": step_id, "fresh_dataset_verified": False}
        for resource in ("attempts", "submissions"):
            data = self.get(resource, step=step_id, user=self.user_id, page=1)
            rows = data.get(resource)
            if not isinstance(rows, list):
                raise APIError("audit_schema")
            shapes = []
            for row in rows[:3]:
                shape = {key: type(row[key]).__name__ for key in ("id", "step", "user", "attempt", "status", "score", "time") if key in row}
                for field in ("dataset", "reply"):
                    value = row.get(field)
                    if isinstance(value, dict):
                        shape[field] = {k: type(v).__name__ for k, v in value.items()}
                        if isinstance(value.get("reply"), dict):
                            shape["reply_inner"] = {k: type(v).__name__ for k, v in value["reply"].items()}
                    else:
                        shape[field] = type(value).__name__
                if resource == "submissions":
                    shape["verdict"] = row.get("status") if row.get("status") in TERMINAL | {"evaluation"} else "unknown"
                shapes.append(shape)
            result[resource] = shapes
        return result

    def verdict(self, submission_id, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = one(self.get(f"submissions/{submission_id}"), "submissions")
            if result.get("status") in TERMINAL:
                return result
            time.sleep(3)
        raise APIError("verdict_timeout")


def unavailable(item):
    if item.get("is_active") is False or item.get("is_enabled") is False:
        return True
    now = datetime.now(timezone.utc)
    for field, future in (("begin_date", True), ("hard_deadline", False), ("end_date", False)):
        if item.get(field):
            try:
                stamp = parse_time(item[field])
            except ValueError:
                return True
            if (future and stamp > now) or (not future and stamp < now):
                return True
    return False


def is_solved(step, api):
    progress = step.get("progress")
    if not isinstance(progress, dict):
        pid = step.get("progress_id") or (progress if isinstance(progress, str) else None)
        if not pid:
            return None
        progress = one(api.get(f"progresses/{pid}"), "progresses")
    flags = [progress[x] for x in ("is_passed", "is_solved") if x in progress]
    if any(x is True for x in flags):
        return True
    if flags and all(x is False for x in flags):
        return False
    return None
