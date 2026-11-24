"""Resumable GET-only inventory. SQLite contains metadata, never task answers."""
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from solver import SUPPORTED, task_reason
from stepik import APIError, is_solved, one, parse_time, unavailable
from storage import load_state, save_state, state_lock

VERSION = 1
EXPECTED = {"exam", "section_unavailable", "unit_unavailable", "resource_unavailable",
            "unsupported_type", "step_unavailable", "limited_submissions", "review_required",
            "external_or_visual_material", "no_python3_template", "already_solved",
            "prior_correct_submission", "prior_unknown_or_pending_verdict", "previously_solved_locally"}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def error_code(exc):
    value = str(exc)
    return value if isinstance(exc, (APIError, ValueError, RuntimeError)) and re.fullmatch(r"[a-z_0-9]+", value) else type(exc).__name__


def audit_scope(course_ids=None):
    ids = sorted(set(course_ids or []))
    if any(type(cid) is not int or cid <= 0 for cid in ids):
        raise ValueError("invalid_audit_course_ids")
    return {"kind": "courses" if ids else "all", "course_ids": ids}


def validate_inventory(report, account, course_ids=None, now=None, step_id=None):
    if (report.get("account") != account or not report.get("inventory_complete")
        or report.get("audit_version") != VERSION or report.get("read_errors")
        or report.get("stop_reason") != "inventory_exhausted"):
        raise ValueError("complete_read_only_account_audit_required")
    requested = audit_scope(course_ids)
    covered = report.get("audit_scope", {"kind": "all", "course_ids": []})
    if requested["kind"] == "courses" and not set(requested["course_ids"]) <= {c["id"] for c in report.get("course_list", [])}:
        raise ValueError("requested_course_not_in_completed_audit")
    if covered["kind"] == "courses" and requested != covered:
        raise ValueError("completed_audit_does_not_cover_requested_courses")
    if covered["kind"] not in {"all", "courses"}:
        raise ValueError("unknown_audit_scope")
    if requested["kind"] == "all" and not report.get("account_inventory_complete", covered["kind"] == "all"):
        raise ValueError("complete_account_audit_required_for_unscoped_writes")
    age = ((now or datetime.now(timezone.utc)) - parse_time(report.get("finished_at"))).total_seconds()
    if not 0 <= age <= 86400:
        raise ValueError("recent_read_only_account_audit_required")
    if step_id is not None and not any(x.get("step") == step_id and x.get("reason") == "candidate_dataset_not_checked" for x in report.get("items", [])):
        raise ValueError("selected_step_not_in_completed_audit")


class AuditStop(RuntimeError):
    pass


class Checkpoint:
    def __init__(self, path, account, parameters, restart=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.is_symlink():
            raise ValueError("audit_checkpoint_symlink_forbidden")
        # Ensure SQLite creates WAL/SHM with the same private permissions.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (page INTEGER PRIMARY KEY, signature TEXT NOT NULL, has_next INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, attempt INTEGER NOT NULL, step INTEGER NOT NULL, status TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS history_step ON history(step);
            CREATE TABLE IF NOT EXISTS ever_correct (step INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS sections (key TEXT PRIMARY KEY, course INTEGER NOT NULL, section INTEGER NOT NULL,
                manifest TEXT NOT NULL, items TEXT NOT NULL, exclusions TEXT NOT NULL, checked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS courses (id INTEGER PRIMARY KEY, manifest TEXT NOT NULL, completed_at TEXT NOT NULL);
        """)
        binding = {"version": VERSION, "account": account, "parameters": parameters}
        old = self.get("binding")
        if old is not None and old != binding and not restart:
            self.db.close()
            raise ValueError("audit_checkpoint_account_or_parameters_mismatch")
        if restart:
            if old and old.get("account") != account:
                self.db.close()
                raise ValueError("audit_checkpoint_belongs_to_another_account")
            with self.db:
                for table in ("pages", "history", "sections", "courses", "meta"):
                    self.db.execute("DELETE FROM " + table)
        with self.db:
            self.put("binding", binding)
            if self.get("created_at") is None:
                self.put("created_at", stamp())

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def invalidate_history(self):
        with self.db:
            self.db.execute("DELETE FROM pages")
            self.db.execute("DELETE FROM history")
            self.db.execute("DELETE FROM sections")
            self.db.execute("DELETE FROM courses")
            self.put("history_complete", False)
            self.put("history_invalidations", self.get("history_invalidations", 0) + 1)

    def close(self):
        self.db.close()


def history_page(api, page):
    response = api.get("submissions", user=api.user_id, status="correct", page=page)
    rows, meta = response.get("submissions"), response.get("meta", {})
    if not isinstance(rows, list) or type(meta.get("has_next")) is not bool or meta.get("page", page) != page:
        raise APIError("audit_history_schema")
    clean = []
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
            or type(row.get("attempt")) is not int or not isinstance(row.get("status"), str)):
            raise APIError("audit_history_schema")
        clean.append({key: row.get(key) for key in ("id", "attempt", "status", "score", "time")})
    if len({row["id"] for row in clean}) != len(clean):
        raise APIError("audit_duplicate_history_ids")
    return clean, meta["has_next"], digest([clean, meta["has_next"]])


def prepare_history(api, checkpoint, progress, limit):
    """Commit complete verified pages. Unchanged resume reads only boundary pages.

    Head/tail anchors detect moved pagination. Interior verdict changes are also
    checked through fresh per-candidate history, so cached absence is never proof.
    """
    db = checkpoint.db
    previous = db.execute("SELECT page,signature FROM pages ORDER BY page").fetchall()
    first = None
    if previous:
        for page, signature in dict([previous[0], previous[-1]]).items():
            current = history_page(api, page)
            if page == 1:
                first = current
            if current[2] != signature:
                checkpoint.invalidate_history()
                previous = []
                progress("history_invalidated", page=page)
                break
    next_page = previous[-1][0] + 1 if previous else 1
    if not checkpoint.get("history_complete", False):
        page = next_page
        while page <= 10000:
            batch = []
            # Establish the first verified page promptly; afterwards combine
            # five pages into one bounded (up to 100 IDs) owner lookup.
            for current in range(page, min(page + (1 if page == 1 else 5), 10001)):
                limit()
                rows, has_next, signature = first if current == 1 and first else history_page(api, current)
                batch.append((current, rows, has_next, signature))
                if not has_next:
                    break
            all_rows = [row for _, rows, _, _ in batch for row in rows]
            attempts = api.fetch_many("attempts", [row["attempt"] for row in all_rows])
            by_id = {a["id"]: a for a in attempts}
            if any(a.get("user") != api.user_id or type(a.get("step")) is not int for a in attempts):
                raise APIError("audit_history_owner_mismatch")
            if any(row["attempt"] not in by_id for row in all_rows):
                raise APIError("audit_history_attempt_missing")
            for current, rows, has_next, signature in batch:
                with db:
                    for row in rows:
                        sid = by_id[row["attempt"]]["step"]
                        db.execute("INSERT OR REPLACE INTO history VALUES (?,?,?,?)", (row["id"], row["attempt"], sid, row["status"]))
                        if row["status"] == "correct":
                            db.execute("INSERT OR IGNORE INTO ever_correct VALUES (?)", (sid,))
                    db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?)", (current, signature, int(has_next)))
                    checkpoint.put("history_complete", not has_next)
                    checkpoint.put("history_checked_at", stamp())
                progress("history", page=current, resumed_at=next_page, complete=not has_next)
            page = batch[-1][0] + 1
            if not has_next:
                break
        else:
            raise APIError("pagination_limit")
    # Do not declare a changed pagination window complete after a long traversal.
    boundaries = db.execute("SELECT page,signature FROM pages ORDER BY page").fetchall()
    for page, signature in dict([boundaries[0], boundaries[-1]]).items():
        limit()
        if history_page(api, page)[2] != signature:
            checkpoint.invalidate_history()
            raise AuditStop("history_changed_during_audit")
    counts = dict(db.execute("SELECT status,COUNT(*) FROM history GROUP BY status").fetchall())
    return {"history_pages": len(boundaries), "returned_submissions": sum(counts.values()), "status_counts": counts,
            "correct_submissions": counts.get("correct", 0), "history_resumed_at": next_page,
            "history_invalidations": checkpoint.get("history_invalidations", 0)}


def fetch_available(api, kind, ids, exclusions, handled, **context):
    """Only a confirmed 403/404 is availability. Schema/transport errors stay errors."""
    ids = list(dict.fromkeys(ids))
    try:
        return api.fetch_many(kind, ids)
    except APIError as exc:
        if exc.status not in {403, 404} and not str(exc).startswith("incomplete_" + kind + "_"):
            raise
        result = []
        for resource_id in ids:
            try:
                row = one(api.get(f"{kind}/{resource_id}"), kind)
                if row.get("id") != resource_id:
                    raise APIError("audit_resource_identity")
                result.append(row)
            except APIError as individual:
                if individual.status not in {403, 404}:
                    raise
                exclusions.append(dict(reason="resource_unavailable", resource=kind, id=resource_id,
                                       http=individual.status, **context))
        handled.append(dict(reason="batch_read_recovered", resource=kind, **context))
        return result


def hydrate(api, steps):
    pids = [s["progress"] for s in steps if isinstance(s.get("progress"), str)
            and s.get("block", {}).get("name") in SUPPORTED and not task_reason(s)]
    progress = {p["id"]: p for p in api.fetch_many("progresses", pids)}
    return [dict(s, progress=progress[s["progress"]]) if isinstance(s.get("progress"), str) and s["progress"] in progress else s for s in steps]


def audit_unavailable(item):
    for field in ("begin_date", "hard_deadline", "end_date"):
        if item.get(field):
            try:
                parse_time(item[field])
            except (ValueError, TypeError):
                raise APIError("audit_availability_schema") from None
    return unavailable(item)


def fresh_history(api, sid):
    """Validate current step/user filters; cache absence is not used for eligibility."""
    rows = list(api.pages("submissions", user=api.user_id, step=sid, order="asc"))
    if any(type(row.get("attempt")) is not int or not isinstance(row.get("status"), str) for row in rows):
        raise APIError("audit_step_history_schema")
    attempts = api.fetch_many("attempts", [row["attempt"] for row in rows])
    if any(a.get("user") != api.user_id or a.get("step") != sid for a in attempts):
        raise APIError("audit_step_history_filter_mismatch")
    if any(row.get("status") == "correct" for row in rows):
        return "prior_correct_submission"
    if any(row.get("status") not in {"wrong", "partially_correct"} for row in rows):
        return "prior_unknown_or_pending_verdict"
    return None


def evaluate(api, checkpoint, step, course, section, state):
    sid = step["id"]
    item = {"step": sid, "course": course, "section": section, "type": step.get("block", {}).get("name")}
    reason = task_reason(step)
    if not reason and (str(sid) in state["solved"] or str(sid) in state.get("legacy_solved", {})):
        reason = "previously_solved_locally"
    if not reason and checkpoint.db.execute("SELECT 1 FROM ever_correct WHERE step=?", (sid,)).fetchone():
        reason = "prior_correct_submission"
    if not reason:
        progress = is_solved(step, api)
        if progress is None:
            raise APIError("audit_progress_schema")
        reason = "already_solved" if progress else fresh_history(api, sid)
    item["reason"] = reason or "candidate_dataset_not_checked"
    return item


def run_inventory(api, state_path, *, expected_user, target=26, max_steps=100000,
                  max_candidates=100000, max_runtime=39600, restart=False, course_ids=None):
    state_path = Path(state_path)
    scope = audit_scope(course_ids)
    started = time.monotonic()
    report = {"audit_version": VERSION, "mode": "dry_run", "started_at": stamp(), "utc_day": stamp()[:10],
              "target": target, "accepted": 0, "submissions_sent": 0, "scanned": 0, "candidates": 0,
              "items": [], "expected_exclusions": [], "read_errors": [], "handled_api_errors": [],
              "inventory_complete": False, "account_inventory_complete": False, "audit_scope": scope,
              "stop_reason": "inventory_exhausted", "resumed_sections": 0, "api_samples": []}
    cp = None
    last_progress = [0.0]

    def progress(stage, **fields):
        data = dict(stage=stage, at=stamp(), scanned=report["scanned"], candidates=report["candidates"], submissions_sent=0, audit_scope=scope, **fields)
        save_state(state_path.parent / "reports/inventory-progress.json", data)
        now = time.monotonic()
        if now - last_progress[0] >= 15 or fields.get("complete") or stage == "history_invalidated":
            print(json.dumps({"inventory_progress": data}), flush=True)
            last_progress[0] = now

    def limit():
        if time.monotonic() - started >= max_runtime:
            raise AuditStop("runtime_limit")
        if report["scanned"] >= max_steps:
            raise AuditStop("scan_limit")
        if report["candidates"] >= max_candidates:
            raise AuditStop("candidate_limit")

    def append(items):
        for item in items:
            if item["step"] in seen_steps:
                continue
            limit()
            seen_steps.add(item["step"])
            report["items"].append(item)
            report["scanned"] += 1
            report["candidates"] += item["reason"] == "candidate_dataset_not_checked"

    seen_steps = set()
    with state_lock(state_path.parent):
        # Publish an incomplete marker before any remote reads. SIGKILL/reboot
        # must not leave an older successful report authorizing a new write.
        save_state(state_path.parent / "reports/latest-dry-run.json", dict(report, stop_reason="audit_in_progress"))
        try:
            api.read_only = True
            account = api.identity()
            if expected_user is None or account != expected_user:
                raise ValueError("unexpected_oauth_account")
            report["account"] = account
            state = load_state(state_path)
            if state["account"] not in (None, account):
                raise ValueError("state_belongs_to_another_account")
            report["accepted"] = sum(x.get("date") == report["utc_day"] and x.get("verified") is True for x in state["solved"].values())
            parameters = {"max_steps": max_steps, "max_candidates": max_candidates, "target": target,
                          "eligibility_policy": "strict-new-v1", "history_filter": "correct_plus_legacy"}
            filename = "audit.sqlite3"
            if scope["kind"] == "courses":
                parameters.update(course_ids=scope["course_ids"], history_filter="per_step_fresh")
                filename = "audit-courses-" + digest(scope["course_ids"])[:12] + ".sqlite3"
            cp = Checkpoint(state_path.parent / filename, account, parameters, restart=restart)
            report["checkpoint_binding"] = digest(cp.get("binding"))
            if scope["kind"] == "all":
                report["history_inventory"] = prepare_history(api, cp, progress, limit)
            else:
                # For an explicitly selected course, verify every eligible step's
                # own full history instead of replaying the unrelated account archive.
                report["history_inventory"] = {"mode": "per_step_fresh", "global_history_replayed": False}
            courses = api.courses()
            report["total_account_courses"] = len(courses)
            if scope["kind"] == "courses":
                selected = set(scope["course_ids"])
                if not selected <= {c["id"] for c in courses}:
                    raise ValueError("selected_course_not_enrolled")
                courses = [c for c in courses if c["id"] in selected]
            report["courses"] = len(courses)
            report["course_list"] = [{"id": c["id"], "title": c.get("title", "")} for c in courses]
            for index, course in enumerate(courses, 1):
                limit()
                cid = course["id"]
                progress("course", course=cid, course_number=index, courses=len(courses))
                course_ok = True
                try:
                    sections = fetch_available(api, "sections", course.get("sections", []), report["expected_exclusions"], report["handled_api_errors"], course=cid)
                except APIError as exc:
                    report["read_errors"].append({"course": cid, "reason": error_code(exc)})
                    if exc.status in {401, 429}:
                        raise AuditStop("api_access_or_rate_limit")
                    continue
                for section in sections:
                    limit()
                    section_id = section["id"]
                    context = {"course": cid, "section": section_id}
                    if section.get("is_exam") or section.get("has_proctor_session"):
                        report["expected_exclusions"].append(dict(reason="exam", **context))
                        continue
                    if audit_unavailable(section):
                        report["expected_exclusions"].append(dict(reason="section_unavailable", **context))
                        continue
                    exclusions = []
                    try:
                        units = fetch_available(api, "units", section.get("units", []), exclusions, report["handled_api_errors"], **context)
                        allowed = []
                        for unit in units:
                            if audit_unavailable(unit):
                                exclusions.append(dict(reason="unit_unavailable", unit=unit["id"], **context))
                            elif unit.get("lesson"):
                                allowed.append(unit["lesson"])
                        lessons = fetch_available(api, "lessons", allowed, exclusions, report["handled_api_errors"], **context)
                        ids = list(dict.fromkeys(sid for lesson in lessons for sid in lesson.get("steps", [])))
                        manifest = digest([section, units, lessons, [audit_unavailable(u) for u in units]])
                        key = f"{cid}:{section_id}"
                        cached = cp.db.execute("SELECT manifest,items,exclusions FROM sections WHERE key=?", (key,)).fetchone()
                        # Parent timestamps do not prove child immutability. Refresh
                        # batched step metadata, then reuse only unchanged exclusions.
                        old = {x["step"]: x for x in json.loads(cached[1])} if cached and cached[0] == manifest else {}
                        raw_steps = fetch_available(api, "steps", ids, exclusions, report["handled_api_errors"], **context)
                        if hasattr(api, "audit_step"):
                            for step in raw_steps:
                                kind = step.get("block", {}).get("name")
                                if kind in SUPPORTED and len(report["api_samples"]) < 20 and sum(x["type"] == kind for x in report["api_samples"]) < 3:
                                    sample = api.audit_step(step["id"])
                                    sample.update(type=kind, checked_at=stamp())
                                    report["api_samples"].append(sample)
                        hashes = {s["id"]: digest(s) for s in raw_steps}
                        pending = [s for s in raw_steps if s["id"] not in old
                                   or old[s["id"]].get("source_fingerprint") != hashes[s["id"]]
                                   or old[s["id"]]["reason"] in {"candidate_dataset_not_checked", "prior_unknown_or_pending_verdict"}]
                        fresh = hydrate(api, pending)
                        refreshed = {}
                        for step in fresh:
                            limit()
                            item = evaluate(api, cp, step, cid, section_id, state)
                            item["source_fingerprint"] = hashes[step["id"]]
                            refreshed[step["id"]] = item
                        items = [refreshed[s["id"]] if s["id"] in refreshed else old[s["id"]] for s in raw_steps]
                        if old:
                            report["resumed_sections"] += 1
                        with cp.db:
                            cp.db.execute("INSERT OR REPLACE INTO sections VALUES (?,?,?,?,?,?,?)", (key, cid, section_id, manifest,
                                          json.dumps(items), json.dumps(exclusions), stamp()))
                        append(items)
                        report["expected_exclusions"].extend(exclusions)
                        progress("section", course=cid, section=section_id)
                    except APIError as exc:
                        course_ok = False
                        report["read_errors"].append(dict(reason=error_code(exc), **context))
                        if exc.status in {401, 429}:
                            raise AuditStop("api_access_or_rate_limit")
                if course_ok:
                    with cp.db:
                        cp.db.execute("INSERT OR REPLACE INTO courses VALUES (?,?,?)", (cid, digest(course), stamp()))
            if report["read_errors"]:
                report["stop_reason"] = "unresolved_read_errors"
            else:
                report["inventory_complete"] = True
                report["account_inventory_complete"] = scope["kind"] == "all"
        except KeyboardInterrupt:
            report["stop_reason"] = "interrupted"
            raise
        except AuditStop as exc:
            report["stop_reason"] = error_code(exc)
        except Exception as exc:
            report["stop_reason"] = error_code(exc)
            report["read_errors"].append({"reason": error_code(exc)})
            raise
        finally:
            if cp:
                cp.close()
            report["finished_at"] = stamp()
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["shortfall"] = max(0, target - report["accepted"])
            report["reasons"] = dict(Counter(x["reason"] for x in report["items"] + report["expected_exclusions"] + report["read_errors"]))
            report["expected_exclusion_reasons"] = dict(Counter(x["reason"] for x in report["items"] + report["expected_exclusions"] if x["reason"] in EXPECTED))
            report["read_error_reasons"] = dict(Counter(x["reason"] for x in report["read_errors"]))
            report["candidate_types"] = dict(Counter(x["type"] for x in report["items"] if x["reason"] == "candidate_dataset_not_checked"))
            report["traversal_skips"] = report["expected_exclusions"] + report["read_errors"]
            report["candidate_days_upper_bound"] = report["candidates"] // 26 if report["inventory_complete"] else None
            report["inventory_note"] = "Counts apply only to audit_scope. Potential new candidates, not guaranteed solvable tasks. No fresh attempts or LLM calls."
            report["per_course"] = {}
            for item in report["items"]:
                group = report["per_course"].setdefault(str(item["course"]), {"types": {}, "reasons": {}, "candidates": 0})
                for field in ("type", "reason"):
                    key = "types" if field == "type" else "reasons"
                    group[key][item[field]] = group[key].get(item[field], 0) + 1
                group["candidates"] += item["reason"] == "candidate_dataset_not_checked"
            folder = state_path.parent / "reports"
            save_state(folder / (report["started_at"].replace(":", "-") + ".json"), report)
            save_state(folder / "latest-dry-run.json", report)
            print(json.dumps({k: report[k] for k in ("mode", "audit_scope", "scanned", "candidates", "candidate_types", "inventory_complete", "account_inventory_complete", "submissions_sent", "stop_reason")}), flush=True)
    return report
