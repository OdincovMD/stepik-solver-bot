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
from stepik import APIError, Stepik, is_solved, parse_time, one
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
    if hasattr(api, "verify_submission_owner"):
        api.verify_submission_owner(job["attempt_id"], int(sid))
        job["account_reverified"] = True
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


def generation_retry_allowed(job, course_id):
    # Only one explicit retry after a local format rejection. A possible POST,
    # verdict, pre-existing submission or prior retry can never pass this gate.
    return (job.get("phase") == "failed" and job.get("reason") == "invalid_choices"
            and job.get("canary") is True and job.get("course") == course_id
            and type(job.get("attempt_id")) is int and job["attempt_id"] > 0
            and job.get("baseline") == [] and job.get("generation_retries", 0) == 0
            and all(k not in job for k in ("sent_at", "submission_id", "reply")))


def wrong_retry_feedback(api, job, course_id, step_id):
    """An explicit new trial may follow a fully read-back wrong, never an uncertain POST."""
    if (job.get("phase") != "done" or job.get("reason") != "wrong"
            or job.get("canary") is not True or job.get("course") != course_id
            or type(job.get("wrong_retries", 0)) is not int or not 0 <= job.get("wrong_retries", 0) < 3
            or type(job.get("submission_id")) is not int or not job.get("sent_at")):
        raise ValueError("wrong_retry_not_safe")
    attempt = api.existing_attempt(job["attempt_id"], step_id)
    result = api.verdict(job["submission_id"])
    if (result.get("id") != job["submission_id"] or result.get("attempt") != job["attempt_id"]
            or result.get("status") != "wrong" or normalized_reply(result) != job.get("reply")):
        raise ValueError("wrong_retry_verdict_changed")
    rows = api.attempt_submissions(job["attempt_id"])
    expected_ids = set(job.get("baseline", [])) | {job["submission_id"]}
    if (len(rows) != len(expected_ids) or {r.get("id") for r in rows} != expected_ids
            or any(r.get("status") != "wrong" or r.get("attempt") != job["attempt_id"] for r in rows)):
        raise ValueError("wrong_retry_history_changed")
    reply = validate_reply({"block": {"name": "choice"}}, attempt, job.get("reply"))
    return {"status": "wrong", "selected_options": [option for option, chosen in
             zip(attempt["dataset"]["options"], reply["choices"]) if chosen]}


def lesson_reference(api, step, options=()):
    """Read earlier theory in this lesson; return complete ranked sentences, never graded replies."""
    import re
    from html import unescape
    lesson_id = step.get("lesson")
    if type(lesson_id) is not int or lesson_id <= 0:
        raise ValueError("lesson_context_identity_missing")
    lesson = one(api.get(f"lessons/{lesson_id}"), "lessons")
    ids = lesson.get("steps", [])
    if lesson.get("id") != lesson_id or step["id"] not in ids:
        raise ValueError("lesson_context_identity_mismatch")
    ids = ids[:ids.index(step["id"])]
    terms = {word[:8] for word in re.findall(r"[^\W\d_]{6,}", step["block"].get("text", "").casefold())}
    terms -= {"тестиров", "следует", "отнести", "которые", "выберите"}
    option_terms = {word[:8] for word in re.findall(r"[^\W\d_]{6,}", " ".join(options).casefold())} - {"тестиров"}
    ranked = []
    for source in api.fetch_many("steps", ids):
        if source.get("lesson") != lesson_id:
            raise ValueError("lesson_context_step_mismatch")
        if source.get("block", {}).get("name") != "text":
            continue
        raw = source["block"].get("text", "")
        raw = re.sub(r"<(?:br\s*/?|/p|/div|/li|/h[1-6])\s*>", "\n", raw, flags=re.I)
        text = unescape(re.sub(r"<[^>]+>", "", raw))
        paragraphs = [x.strip() for x in text.splitlines() if x.strip()]
        title = paragraphs[0] if paragraphs and len(paragraphs[0]) <= 130 else ""
        # Keep the antecedent of course statements such as "both types belong
        # to ...". A detached classification sentence loses which types it covers.
        relations = [p for p in paragraphs if re.search(r"\b(?:относятся|относится)\b", p, re.I)
                     and not re.search(r"\bне\s+отно", p, re.I)
                     and any(term in p.casefold() for term in terms)]
        for relation in relations:
            categories = [word for word in re.findall(r"[^\W\d_]+", relation)
                          if any(word.casefold().startswith(term) for term in terms)]
            if not categories:
                continue
            for paragraph in paragraphs:
                parts = re.split(r"\s+[–—−-]\s+", paragraph, maxsplit=1)
                if len(parts) != 2:
                    continue
                subject = re.sub(r"\([^)]*\)", "", parts[0]).strip()
                score = sum(term in subject.casefold() for term in option_terms)
                if score and len(subject) <= 120:
                    fact = subject + ": " + ", ".join(categories) + " тестирование."
                    ranked.append((20 + score, source["id"], fact))
        for paragraph in paragraphs:
            for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
                if not sentence.strip():
                    continue
                score = 4 * sum(term in (title + " " + sentence).casefold() for term in terms)
                score += sum(term in sentence.casefold() for term in option_terms)
                if score:
                    note = sentence if not title or title == sentence else title + ": " + sentence
                    ranked.append((score, source["id"], note))
    ranked.sort(key=lambda x: (-x[0], len(x[2].encode())))
    if not ranked:
        raise ValueError("lesson_context_no_relevant_theory")
    return [{"source_step": sid, "text": text} for score, sid, text in ranked[:20]]


def review_generation_source(state, sid, course_id):
    job = state["jobs"].get(sid, {})
    history = state.get("job_history", {}).get(sid, [])
    if (job.get("phase") != "failed" or job.get("reason") not in {
            "model_abstained_or_invalid_json", "invalid_choices", "model_repeated_rejected_reply",
            "model_output_truncated_or_incomplete"}
            or job.get("canary") is not True or job.get("course") != course_id
            or not history or history[-1].get("submission_id") != job.get("retry_of")
            or type(job.get("attempt_id")) is not int or job["attempt_id"] <= 0
            or type(job.get("review_retries", 0)) is not int or not 0 <= job.get("review_retries", 0) < 2
            or not isinstance(job.get("baseline"), list)
            or any(key in job for key in ("sent_at", "submission_id", "reply"))):
        raise ValueError("review_generation_retry_not_safe")
    return history[-1]


def run(api, solver, state_path, target=26, dry_run=True, *, max_submissions=104,
        max_candidates=104, max_steps=10000, max_runtime=39600, guard_seconds=1200,
        expected_user=None, canary=False, require_canary=False, model_digest=None, recover_only=False, check_model=False,
        trial=False, require_trial=False, course_ids=None, candidate_step_ids=None, resume_generation=False,
        retry_wrong=False, use_lesson_context=False, resume_review=False):
    if (resume_generation or retry_wrong) and (dry_run or not canary or not course_ids or len(course_ids) != 1
                              or candidate_step_ids is None or len(candidate_step_ids) != 1):
        raise ValueError("generation_retry_requires_exact_canary_scope")
    if resume_generation and retry_wrong:
        raise ValueError("retry_modes_are_mutually_exclusive")
    if use_lesson_context and not retry_wrong:
        raise ValueError("lesson_context_requires_explicit_wrong_retry")
    if resume_review and not retry_wrong:
        raise ValueError("review_resume_requires_explicit_wrong_retry")
    if dry_run and isinstance(api, Stepik):
        from audit import run_inventory
        report = run_inventory(api, state_path, expected_user=expected_user if expected_user is not None else api.identity(),
                               target=target, max_steps=max_steps, max_candidates=max_candidates, max_runtime=max_runtime,
                               course_ids=course_ids)
        return report["accepted"]
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
                if getattr(api, "inventory_correct_steps", None) is not None or getattr(api, "read_only", False):
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
                if resume_generation:
                    retry_sid = str(next(iter(candidate_step_ids)))
                    if not generation_retry_allowed(state["jobs"].get(retry_sid, {}), course_ids[0]):
                        raise ValueError("generation_retry_not_safe")
                if retry_wrong:
                    retry_sid = str(next(iter(candidate_step_ids)))
                    retry_previous = state["jobs"].get(retry_sid, {})
                    if resume_review:
                        retry_previous = review_generation_source(state, retry_sid, course_ids[0])
                    retry_feedback = wrong_retry_feedback(api, retry_previous, course_ids[0], int(retry_sid))
                    rejected = [retry_feedback["selected_options"]]
                    for previous in state.get("job_history", {}).get(retry_sid, []):
                        if previous.get("reason") == "wrong":
                            prior = wrong_retry_feedback(api, previous, course_ids[0], int(retry_sid))
                            if sorted(prior["selected_options"]) not in [sorted(x) for x in rejected]:
                                rejected.append(prior["selected_options"])
                    if len(rejected) > 1:
                        retry_feedback["rejected_selections"] = rejected
                    if use_lesson_context:
                        previous_attempt = api.existing_attempt(retry_previous["attempt_id"], int(retry_sid))
                        retry_feedback["reference"] = lesson_reference(api, api.step(int(retry_sid)), previous_attempt["dataset"]["options"])
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
            report["total_account_courses"] = len(courses)
            if course_ids:
                selected = set(course_ids)
                if not selected <= {c["id"] for c in courses}:
                    raise ValueError("selected_course_not_enrolled")
                courses = [c for c in courses if c["id"] in selected]
            report["audit_scope"] = {"kind": "courses" if course_ids else "all", "course_ids": sorted(set(course_ids or []))}
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
                if candidate_step_ids is not None and step["id"] not in candidate_step_ids:
                    item["reason"] = "not_in_completed_audit"
                    continue
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
                retry_job = state["jobs"].get(sid) if resume_generation else None
                if state["attempted"].get(sid) == day and retry_job is None and not retry_wrong:
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
                    if attempts_today >= max_candidates and retry_job is None and not retry_wrong:
                        report["stop_reason"] = "daily_attempt_limit"
                        item["reason"] = "daily_attempt_limit"
                        break
                    if retry_job is not None:
                        if not generation_retry_allowed(retry_job, course["id"]):
                            raise ValueError("generation_retry_not_safe")
                        job = retry_job
                        # Reserve before GET/generation: a crash consumes the retry.
                        job.update(phase="solving", generation_retries=1, model_digest=model_digest)
                        save_state(state_path, state)
                        attempt = api.existing_attempt(job["attempt_id"], step["id"])
                        if api.attempt_submissions(attempt["id"]):
                            raise ValueError("generation_retry_submission_exists")
                        item["reused_attempt"] = True
                    elif resume_review:
                        review_generation_source(state, sid, course["id"])
                        job = state["jobs"][sid]
                        job.update(phase="solving", review_retries=job.get("review_retries", 0) + 1, model_digest=model_digest)
                        save_state(state_path, state)
                        attempt = api.existing_attempt(job["attempt_id"], step["id"])
                        rows = api.attempt_submissions(attempt["id"])
                        if ({r.get("id") for r in rows} != set(job["baseline"])
                                or len(rows) != len(job["baseline"])
                                or any(r.get("status") != "wrong" or r.get("attempt") != attempt["id"] for r in rows)):
                            raise ValueError("review_generation_history_changed")
                        item["reused_attempt"] = True
                    else:
                        job = {"phase": "creating", "day": day, "course": course["id"],
                               "canary": canary, "trial": trial, "model_digest": model_digest}
                        if retry_wrong:
                            import copy
                            state.setdefault("job_history", {}).setdefault(sid, []).append(copy.deepcopy(retry_previous))
                            job.update(wrong_retries=retry_previous.get("wrong_retries", 0) + 1,
                                       retry_of=retry_previous["submission_id"])
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
                    if retry_wrong:
                        reply = validate_reply(step, attempt, solver.solve(step, attempt, feedback=retry_feedback))
                    else:
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
                    if job is not None and job.get("phase") == "solving":
                        item["model_metrics"] = getattr(solver, "metrics", {})
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
    parser.add_argument("--restart-audit", action="store_true", help="Discard read-only cursors for this account, keeping verified old correct exclusions")
    parser.add_argument("--course-id", action="append", type=int, help="Explicit course scope for read-only audit and canary/trial")
    parser.add_argument("--step-id", type=int, help="Select exactly one audited step; canary mode only")
    parser.add_argument("--resume-generation", action="store_true", help="One retry of a never-submitted canary rejected locally as invalid_choices; same attempt only")
    parser.add_argument("--retry-wrong", action="store_true", help="Explicit single canary POST after a fully verified wrong; same selected task, prior verdict given to LLM")
    parser.add_argument("--lesson-context", action="store_true", help="Ground an explicit wrong retry in earlier theory of the same lesson")
    parser.add_argument("--resume-review", action="store_true", help="Bounded generation retry of a reviewed canary that has never sent a POST")
    args = parser.parse_args()
    if (args.resume_generation or args.retry_wrong) and not (args.canary and args.step_id and args.course_id and len(args.course_id) == 1):
        parser.error("retry requires --canary, one --course-id and --step-id")
    if args.resume_generation and args.retry_wrong:
        parser.error("retry modes are mutually exclusive")
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
        if args.course_id and not (dry_run or args.canary or args.trial):
            raise ValueError("course_scope_only_for_audit_and_trials")
        if args.step_id is not None and (not args.canary or args.step_id <= 0):
            raise ValueError("step_selection_only_for_canary")
        if dry_run:
            from audit import run_inventory
            report = run_inventory(
                api, state_path, expected_user=expected, target=target, max_steps=args.max_steps,
                max_candidates=args.max_candidates, max_runtime=args.max_runtime, restart=args.restart_audit,
                course_ids=args.course_id)
            return 0 if report["inventory_complete"] else 3
        solver = configured_model()
        audited_candidates = None
        if not dry_run and not args.reconcile:
            audit_path = state_path.parent / "reports/latest-dry-run.json"
            if not audit_path.exists():
                raise ValueError("read_only_account_audit_required")
            audit = json.loads(audit_path.read_text())
            from audit import validate_inventory
            validate_inventory(audit, expected, args.course_id, now=utcnow(), step_id=args.step_id)
            audited_candidates = {x["step"] for x in audit.get("items", []) if x.get("reason") == "candidate_dataset_not_checked"}
            if args.step_id is not None:
                audited_candidates = {args.step_id}
        result = run(api, solver, state_path, target, dry_run,
                     max_submissions=args.max_submissions, max_candidates=args.max_candidates,
                     max_steps=args.max_steps, max_runtime=args.max_runtime, expected_user=expected,
                     canary=args.canary, require_canary=args.require_canary, trial=args.trial,
                     require_trial=args.submit or args.require_trial,
                     recover_only=args.reconcile, check_model=True, course_ids=args.course_id,
                     candidate_step_ids=audited_candidates, resume_generation=args.resume_generation,
                     retry_wrong=args.retry_wrong, use_lesson_context=args.lesson_context, resume_review=args.resume_review)
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
