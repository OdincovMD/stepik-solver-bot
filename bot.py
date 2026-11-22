"""Daily local Stepik practice. Default mode is a read-only inventory."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import DEFAULT_ENV, DEFAULT_STATE, load_env
from solver import SUPPORTED, configured_model, task_reason, validate_reply
from stepik import APIError, Stepik, is_solved, parse_time
from storage import load_state, save_state, state_lock


def utcnow():
    return datetime.now(timezone.utc)


def safe_error(exc):
    # Only allow our own reason codes. Third-party exception text may contain secrets.
    value = str(exc)
    import re
    if isinstance(exc, (APIError, ValueError, RuntimeError)) and re.fullmatch(r"[a-z_0-9]+", value):
        return value
    return type(exc).__name__


def count_today(state, day):
    return sum(x.get("date") == day and x.get("verified") is True for x in state["solved"].values())


def full_score(result):
    from decimal import Decimal, InvalidOperation
    try:
        return result.get("status") == "correct" and Decimal(str(result.get("score"))) == 1
    except InvalidOperation:
        return False


def can_start(day, guard_seconds):
    now = utcnow()
    remaining = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
    return now.date().isoformat() == day and remaining > guard_seconds


def confirm(api, state, sid, job):
    """No increment until server verdict, full score, time and step progress agree."""
    result = api.verdict(job["submission_id"])
    if result.get("id") != job["submission_id"] or result.get("attempt") != job["attempt_id"]:
        raise APIError("submission_identity")
    if result.get("status") == "correct":
        from decimal import Decimal, InvalidOperation
        try:
            score = Decimal(str(result.get("score")))
            if not score.is_finite() or not 0 <= score <= 1:
                raise InvalidOperation()
        except InvalidOperation:
            raise APIError("correct_score_unknown") from None
    if not full_score(result):
        if result.get("status") not in {"correct", "wrong", "partially_correct"}:
            raise APIError("unknown_verdict")
        job.update(phase="done", reason="partial_score" if result.get("status") == "correct" else result["status"])
        return
    if is_solved(api.step(int(sid)), api) is not True:
        raise APIError("correct_awaiting_progress")
    try:
        stamp = parse_time(result.get("time"))
        sent = parse_time(job["sent_at"])
    except ValueError:
        raise APIError("correct_without_server_time") from None
    # Date comes from the server, never the run start or restart date.
    if stamp < sent.replace(microsecond=0) or stamp > utcnow():
        raise APIError("server_time_inconsistent")
    state["solved"][sid] = {
        "date": stamp.date().isoformat(), "server_time": stamp.isoformat(),
        "submission_id": result["id"], "verified": True,
    }
    job.update(phase="done", reason="accepted", server_time=stamp.isoformat())
    if job.get("canary"):
        state["canary"] = {"account": state["account"], "step": int(sid),
                           "submission_id": result["id"], "model_digest": job.get("model_digest"),
                           "verified_at": utcnow().isoformat()}
    if job.get("trial"):
        marker = state.get("trial", {})
        if marker.get("account") != state["account"] or marker.get("model_digest") != job.get("model_digest"):
            marker = {"account": state["account"], "model_digest": job.get("model_digest"), "steps": {}}
        marker["steps"][sid] = result["id"]
        state["trial"] = marker


def normalized_reply(row):
    value = row.get("reply")
    # Official clients have exposed both direct replies and ReplyWrapper objects.
    return value.get("reply") if isinstance(value, dict) and set(value) == {"reply"} else value


def recover(api, state, path, report):
    """An uncertain POST is only read back. It is never automatically repeated."""
    for sid, job in state["jobs"].items():
        if job["phase"] in {"done", "failed"}:
            continue
        if job["phase"] != "sending":
            job.update(phase="failed", reason="interrupted_before_submission")
            save_state(path, state)
            continue
        try:
            if not job.get("submission_id"):
                rows = api.attempt_submissions(job["attempt_id"])
                rows = [x for x in rows if x.get("id") not in job.get("baseline", [])]
                matches = [x for x in rows if x.get("attempt") == job["attempt_id"] and normalized_reply(x) == job["reply"]]
                # Even an empty list may mean a delayed server write. Keep reserved.
                if len(rows) != 1 or len(matches) != 1:
                    raise APIError("ambiguous_submission_do_not_resend")
                job["submission_id"] = matches[0]["id"]
                save_state(path, state)
            confirm(api, state, sid, job)
            save_state(path, state)
            report["items"].append({"step": int(sid), "reason": "recovered_" + job["reason"]})
        except (APIError, ValueError, KeyError) as exc:
            report["items"].append({"step": int(sid), "reason": safe_error(exc)})
            report["stop_reason"] = "unresolved_submission"
            return False
    return True


def run(api, solver, state_path, target=26, dry_run=True, *, max_submissions=104,
        max_candidates=104, max_steps=10000, max_runtime=39600, guard_seconds=1200,
        expected_user=None, canary=False, require_canary=False, model_digest=None, recover_only=False, check_model=False,
        trial=False, require_trial=False):
    state_path = Path(state_path)
    report = {"started_at": utcnow().isoformat(), "utc_day": utcnow().date().isoformat(),
              "mode": "dry_run" if dry_run else ("reconcile" if recover_only else ("canary" if canary else ("trial" if trial else "submit"))),
              "target": target, "accepted": 0, "candidates": 0, "submissions_sent": 0,
              "scanned": 0, "items": [], "stop_reason": "inventory_exhausted"}
    day = report["utc_day"]
    state = None
    account_checked = False
    started = time.monotonic()
    with state_lock(state_path.parent):
        try:
            state = load_state(state_path)
            account = api.identity()
            report["account"] = account
            if expected_user is not None and account != expected_user:
                raise RuntimeError("unexpected_oauth_account")
            if state["account"] is not None and state["account"] != account:
                raise RuntimeError("state_belongs_to_another_account")
            account_checked = True
            if not dry_run:
                if getattr(api, "inventory_correct_steps", None) is not None:
                    raise RuntimeError("fresh_stepik_client_required_for_submission")
                if expected_user is None:
                    raise RuntimeError("expected_user_required_for_submission")
                state["account"] = account
                save_state(state_path, state)
                if not recover(api, state, state_path, report):
                    return count_today(state, day)
                if recover_only:
                    report["stop_reason"] = "reconciled"
                    return count_today(state, day)
                if check_model:
                    report["model"] = solver.preflight()
                    model_digest = report["model"].get("digest")
                marker = state.get("canary", {})
                if canary and model_digest and marker.get("account") == account and marker.get("model_digest") == model_digest:
                    report["stop_reason"] = "canary_already_verified"
                    return count_today(state, day)
                if require_canary or trial or require_trial:
                    if marker.get("account") != account or marker.get("model_digest") != model_digest or not model_digest:
                        raise RuntimeError("verified_canary_required_for_this_model")
                if require_trial:
                    tested = state.get("trial", {})
                    if tested.get("account") != account or tested.get("model_digest") != model_digest or len(tested.get("steps", {})) < 3:
                        raise RuntimeError("three_verified_trial_solutions_required")
                    if not marker.get("user_verified"):
                        raise RuntimeError("user_canary_verification_required")
            if not dry_run and count_today(state, day) >= target:
                report["stop_reason"] = "target_reached"
                return count_today(state, day)
            courses = api.courses()
            report["courses"] = len(courses)
            report["course_list"] = [{"id": c["id"], "title": c.get("title", "")} for c in courses]
            report["legacy_unverified"] = len(state.get("legacy_solved", {}))
            if dry_run and hasattr(api, "read_progress"):
                def inventory_progress(course_id):
                    progress = {"at": utcnow().isoformat(), "course": course_id, "courses": len(courses),
                                "scanned": report["scanned"], "candidates": report["candidates"], "submissions_sent": 0}
                    save_state(state_path.parent / "reports/inventory-progress.json", progress)
                    print(json.dumps({"inventory_progress": progress}), flush=True)
                api.read_progress = inventory_progress
                if hasattr(api, "prepare_inventory"):
                    report["history_inventory"] = api.prepare_inventory()
            for step, course in api.steps(courses):
                if time.monotonic() - started >= max_runtime:
                    report["stop_reason"] = "runtime_limit"
                    break
                if report["scanned"] >= max_steps:
                    report["stop_reason"] = "scan_limit"
                    break
                if not dry_run and not can_start(day, guard_seconds):
                    report["stop_reason"] = "utc_day_boundary_guard"
                    break
                if not dry_run and count_today(state, day) >= target:
                    report["stop_reason"] = "target_reached"
                    break
                if not dry_run and report["submissions_sent"] >= (1 if canary else (min(3, max_submissions) if trial else max_submissions)):
                    report["stop_reason"] = "submission_limit"
                    break
                if report["candidates"] >= max_candidates:
                    report["stop_reason"] = "candidate_limit"
                    break
                report["scanned"] += 1
                sid = str(step["id"])
                item = {"step": step["id"], "course": course["id"], "type": step.get("block", {}).get("name")}
                report["items"].append(item)
                # Include solved steps: their history can establish real accepted
                # verdict/reply shapes before any fresh attempt is created.
                sampled = report.get("api_samples", [])
                if dry_run and item["type"] in SUPPORTED and hasattr(api, "audit_step") and len(sampled) < 20 and sum(x.get("type") == item["type"] for x in sampled) < 3:
                    sample = api.audit_step(step["id"])
                    sample["type"] = item["type"]
                    report.setdefault("api_samples", []).append(sample)
                reason = task_reason(step)
                if reason:
                    item["reason"] = reason
                    continue
                if sid in state["solved"] or sid in state.get("legacy_solved", {}):
                    item["reason"] = "previously_solved_locally"
                    continue
                if state["attempted"].get(sid) == day:
                    item["reason"] = "attempted_today"
                    continue
                job = None
                task_started = time.monotonic()
                try:
                    progress = is_solved(step, api)
                    if progress is not False:
                        item["reason"] = "already_solved" if progress else "unknown_progress"
                        continue
                    # History catches stale progress and old full or partial correct submissions.
                    history = api.correct_history(step["id"])
                    if history:
                        item["reason"] = "prior_correct_submission" if any(r.get("status") == "correct" for r in history) else "prior_unknown_or_pending_verdict"
                        continue
                    report["candidates"] += 1
                    if dry_run:
                        item["reason"] = "candidate_dataset_not_checked"
                        continue
                    # The daily cap includes previous process starts and errors before sending.
                    attempts_today = sum(value == day for value in state["attempted"].values())
                    if attempts_today >= max_candidates:
                        report["stop_reason"] = "daily_attempt_limit"
                        item["reason"] = "daily_attempt_limit"
                        break
                    job = {"phase": "creating", "day": day, "course": course["id"],
                           "canary": canary, "trial": trial, "model_digest": model_digest}
                    state["jobs"][sid] = job
                    state["attempted"][sid] = day
                    save_state(state_path, state)
                    attempt = api.create_attempt(step["id"])
                    job.update(phase="solving", attempt_id=attempt["id"])
                    save_state(state_path, state)
                    # Stepik may reuse an active attempt; retain existing submission IDs.
                    previous = api.attempt_submissions(attempt["id"])
                    if any(x.get("status") not in {"correct", "wrong", "partially_correct"} for x in previous):
                        raise ValueError("attempt_has_pending_submission")
                    job["baseline"] = [x["id"] for x in previous]
                    save_state(state_path, state)
                    reply = validate_reply(step, attempt, solver.solve(step, attempt))
                    item["model_metrics"] = getattr(solver, "metrics", {})
                    # The model can take minutes: recheck both the UTC boundary and live progress.
                    if not can_start(day, guard_seconds) or time.monotonic() - started >= max_runtime:
                        job.update(phase="failed", reason="time_limit_before_submission")
                        item["reason"] = job["reason"]
                        report["stop_reason"] = job["reason"]
                        save_state(state_path, state)
                        break
                    if is_solved(api.step(step["id"]), api) is not False or api.correct_history(step["id"]):
                        job.update(phase="failed", reason="progress_changed_before_submission")
                        item["reason"] = job["reason"]
                        save_state(state_path, state)
                        continue
                    # Progress/history GETs can themselves stall; check again immediately before WAL/POST.
                    if not can_start(day, guard_seconds) or time.monotonic() - started >= max_runtime:
                        job.update(phase="failed", reason="time_limit_before_submission")
                        item["reason"] = job["reason"]
                        report["stop_reason"] = job["reason"]
                        save_state(state_path, state)
                        break
                    job.update(phase="sending", reply=reply, sent_at=utcnow().isoformat())
                    save_state(state_path, state)  # WAL before the non-idempotent POST.
                    report["submissions_sent"] += 1
                    job["submission_id"] = api.submit(attempt["id"], reply)
                    save_state(state_path, state)
                    confirm(api, state, sid, job)
                    item["reason"] = job["reason"]
                    save_state(state_path, state)
                except (APIError, ValueError, KeyError, TypeError, TimeoutError) as exc:
                    item["reason"] = safe_error(exc)
                    if job is not None:
                        if job["phase"] == "sending":
                            if not job.get("submission_id") and isinstance(exc, APIError) and exc.status in {400, 401, 403, 404, 409, 422, 429}:
                                job.update(phase="failed", reason=item["reason"])
                                save_state(state_path, state)
                                if exc.status in {401, 403, 429}:
                                    report["stop_reason"] = "api_access_or_rate_limit"
                                    break
                                continue
                            # Stop: unknown writes reserve quota and must be reconciled first.
                            report["stop_reason"] = "unresolved_submission"
                            break
                        job.update(phase="failed", reason=item["reason"])
                        save_state(state_path, state)
                    if isinstance(exc, APIError) and exc.status in {401, 403, 429}:
                        report["stop_reason"] = "api_access_or_rate_limit"
                        break
                finally:
                    item["wall_seconds"] = round(time.monotonic() - task_started, 3)
            if not dry_run and count_today(state, day) >= target:
                report["stop_reason"] = "target_reached"
            return count_today(state, day)
        except KeyboardInterrupt:
            report["stop_reason"] = "interrupted"
            raise
        except Exception as exc:
            report["stop_reason"] = safe_error(exc)
            raise
        finally:
            report["finished_at"] = utcnow().isoformat()
            report["accepted"] = count_today(state, day) if state and account_checked else 0
            if state and account_checked:
                report["unresolved"] = [int(sid) for sid, job in state["jobs"].items() if job["phase"] == "sending"]
                report["accepted_steps"] = [int(sid) for sid, item in state["solved"].items()
                                             if item.get("date") == day and item.get("verified")]
            report["traversal_skips"] = getattr(api, "notices", [])
            reasons = {}
            for item in report["items"] + report["traversal_skips"]:
                reason = item.get("reason", "interrupted")
                reasons[reason] = reasons.get(reason, 0) + 1
            report["reasons"] = reasons
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["shortfall"] = max(0, target - report["accepted"])
            report["inventory_complete"] = dry_run and report["stop_reason"] == "inventory_exhausted" and not report["traversal_skips"] and not any(i.get("reason", "").endswith("_unavailable") or "_http_" in i.get("reason", "") for i in report["items"])
            report["candidate_days_upper_bound"] = report["candidates"] // 26 if report["inventory_complete"] else None
            report["inventory_note"] = "Potential new tasks; fresh attempt dataset, model accuracy and eventual verdict remain unverified."
            summary = {}
            for item in report["items"]:
                group = summary.setdefault(str(item.get("course", "unknown")), {"types": {}, "reasons": {}, "candidates": 0})
                kind, reason = item.get("type", "unknown"), item.get("reason", "interrupted")
                group["types"][kind] = group["types"].get(kind, 0) + 1
                group["reasons"][reason] = group["reasons"].get(reason, 0) + 1
                group["candidates"] += reason == "candidate_dataset_not_checked"
            report["per_course"] = summary
            reports = state_path.parent / "reports"
            stamp = report["started_at"].replace(":", "-")
            save_state(reports / (stamp + ".json"), report)
            save_state(reports / ("latest-dry-run.json" if dry_run else "latest.json"), report)
            print(json.dumps({k: report[k] for k in ("utc_day", "mode", "accepted", "target", "candidates",
                                                     "submissions_sent", "stop_reason", "reasons")}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="GET-only inventory; default")
    modes.add_argument("--submit", action="store_true", help="Submit answers, up to the UTC daily target")
    modes.add_argument("--canary", action="store_true", help="At most ONE submission; verify full score and progress")
    modes.add_argument("--trial", action="store_true", help="At most THREE answer POSTs after a verified canary")
    modes.add_argument("--approve-canary", action="store_true", help="Record your visual verification in an interactive Pi terminal")
    modes.add_argument("--check-account", action="store_true", help="Read OAuth account id only")
    modes.add_argument("--reconcile", action="store_true", help="Recover pending verdicts without creating attempts or sending answers")
    modes.add_argument("--report", action="store_true", help="Print last saved report without network")
    parser.add_argument("--require-canary", action="store_true")
    parser.add_argument("--require-trial", action="store_true")
    parser.add_argument("--max-submissions", type=int, default=104)
    parser.add_argument("--max-candidates", type=int, default=104)
    parser.add_argument("--max-steps", type=int, default=10000)
    parser.add_argument("--max-runtime", type=int, default=39600)
    args = parser.parse_args()
    try:
        load_env(args.env_file)
        state_path = Path(os.getenv("STATE_DIR", str(DEFAULT_STATE))) / "progress.json"
        if args.approve_canary:
            if not sys.stdin.isatty():
                raise ValueError("interactive_pi_terminal_required")
            with state_lock(state_path.parent):
                state = load_state(state_path)
                marker = state.get("canary", {})
                if not marker.get("submission_id"):
                    raise ValueError("verified_canary_required")
                print("Check Stepik step", marker["step"], "submission", marker["submission_id"])
                if input("After checking full acceptance in Stepik, type the submission ID: ").strip() != str(marker["submission_id"]):
                    raise ValueError("verification_not_recorded")
                marker["user_verified"] = True
                save_state(state_path, state)
            return 0
        if args.report:
            report = json.loads((state_path.parent / "reports/latest.json").read_text())
            state = load_state(state_path)
            report["current_utc_day"] = utcnow().date().isoformat()
            report["current_accepted"] = count_today(state, report["current_utc_day"])
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        client, secret = os.getenv("STEPIK_CLIENT_ID"), os.getenv("STEPIK_CLIENT_SECRET")
        if not client or not secret or client == "replace_me" or secret == "replace_me":
            raise ValueError("local_oauth_credentials_required")
        api = Stepik(client, secret)
        if args.check_account:
            identity = api.identity()
            if identity != int(os.getenv("STEPIK_EXPECTED_USER_ID", "0")):
                raise ValueError("unexpected_oauth_account")
            print("OAuth account id:", identity)
            return
        target = int(os.getenv("DAILY_TARGET", "26"))
        expected = os.getenv("STEPIK_EXPECTED_USER_ID")
        expected = int(expected) if expected else None
        if min(target, args.max_submissions, args.max_candidates, args.max_steps, args.max_runtime) <= 0:
            raise ValueError("limits_must_be_positive")
        dry_run = not (args.submit or args.canary or args.trial or args.reconcile)
        solver = configured_model()
        if not dry_run and not args.reconcile:
            audit_path = state_path.parent / "reports/latest-dry-run.json"
            if not audit_path.exists():
                raise ValueError("read_only_account_audit_required")
            audit = json.loads(audit_path.read_text())
            if audit.get("account") != expected or not audit.get("inventory_complete"):
                raise ValueError("complete_read_only_account_audit_required")
        result = run(api, solver, state_path, target, dry_run,
                     max_submissions=args.max_submissions, max_candidates=args.max_candidates,
                     max_steps=args.max_steps, max_runtime=args.max_runtime, expected_user=expected,
                     canary=args.canary, require_canary=args.require_canary, trial=args.trial,
                     require_trial=args.submit or args.require_trial,
                     recover_only=args.reconcile, check_model=True)
        last = json.loads((state_path.parent / "reports" / ("latest-dry-run.json" if dry_run else "latest.json")).read_text())
        if last["stop_reason"] in {"unresolved_submission", "api_access_or_rate_limit"}:
            return 3
        if args.reconcile:
            return 0
        if args.canary:
            marker = load_state(state_path).get("canary", {})
            return 0 if marker.get("account") == expected and marker.get("model_digest") == last.get("model", {}).get("digest") else 2
        if args.trial:
            return 0 if len(load_state(state_path).get("trial", {}).get("steps", {})) >= 3 else 2
        if not dry_run and result < target:
            return 2
        return 0
    except KeyboardInterrupt:
        print("interrupted_recovery_on_next_run", file=sys.stderr)
        return 130
    except Exception as exc:
        print("bot_error: " + safe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    import signal

    def stop(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop)
    sys.exit(main())
