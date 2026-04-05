#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap.

The task is no longer "does the script mention MinIO?".
It is "does the backup pipeline behave correctly on both clean and dirty data?".

Objectives:
1. pipeline_handles_clean_and_drift_runs
   - A clean run must succeed and update both the persistent status note and
     machine-readable restore handoff.
   - The built-in live drift scenario must fail, report both sides of the drift,
     mark restore handoff unsafe, and preserve scoped MinIO access.
2. clean_run_proves_real_backup_artifacts
   - A clean run must show evidence that a real dump and object capture were
     produced, not just that validation passed, and refresh downstream restore
     handoff details.
3. validation_catches_all_drift_modes
   - Forward gap, reverse gap, equal-count set drift, and wrong-bytes integrity
     drift must all fail closed and be reported. Partial drift handling does
     not get separate credit.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import time

try:
    from apex_arena._types import GradingResult
except ImportError:
    class GradingResult:
        def __init__(self, score, subscores=None, weights=None, feedback=None):
            self.score = score
            self.subscores = subscores or {}
            self.weights = weights or {}
            self.feedback = json.dumps(feedback) if isinstance(feedback, dict) else (feedback or "")


STATUS_CONFIGMAP = "glitchtip-backup-status"
STATUS_KEY = "status.md"
HANDOFF_CONFIGMAP = "glitchtip-backup-restore-handoff"
HANDOFF_KEY = "handoff.json"
RUNTIME_CONFIGMAP_TARGETS = {STATUS_CONFIGMAP, HANDOFF_CONFIGMAP}
MINIO_ACCESS_POLICY = "glitchtip-minio-access-policy"


def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def load_setup_info():
    info = {}
    try:
        with open("/root/.setup_info") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    info[key] = value
    except FileNotFoundError:
        pass
    return info


def content_digest(value):
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def byte_count(value):
    return len(value.encode("utf-8"))


def sql_escape(value):
    return value.replace("'", "''")


def get_pg_pod():
    rc, pod, _ = run_cmd(
        "kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
    )
    return pod.strip("'") if pod else ""


def get_minio_pod():
    rc, pod, _ = run_cmd(
        "kubectl get pods -n glitchtip -l app=glitchtip-minio "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
    )
    return pod.strip("'") if pod else ""


def pg_sql(pg_pod, sql, setup_info):
    pw = setup_info.get("GT_DB_PASS", "7KkJeWZYkK")
    user = setup_info.get("GT_DB_USER", "postgres")
    db = setup_info.get("GT_DB_NAME", "postgres")
    rc, out, _ = run_cmd(
        f"kubectl exec -n glitchtip {pg_pod} -- "
        f"bash -c \"PGPASSWORD={pw} psql -U {user} -d {db} -tAc \\\"{sql}\\\"\"",
        timeout=20,
    )
    return out.strip()


def minio_exec(minio_pod, mc_cmd, setup_info):
    ak = setup_info.get("MINIO_ACCESS_KEY", "")
    sk = setup_info.get("MINIO_SECRET_KEY", "")
    rc, out, err = run_cmd(
        f"kubectl exec -n glitchtip {minio_pod} -- "
        f"sh -c \"mc alias set local http://localhost:9000 '{ak}' '{sk}' "
        f">/dev/null 2>&1; {mc_cmd}\"",
        timeout=20,
    )
    return rc, out, err


def trigger_backup_job(job_name, wait_seconds=300):
    rc, out, stderr = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=20,
    )
    if rc != 0:
        return False, "", "", f"Could not create job: {(stderr or out)[:200]}"

    completed = False
    failed_count = ""
    for _ in range(int(wait_seconds / 10)):
        rc, status, _ = run_cmd(
            f"kubectl get job {job_name} -n glitchtip "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}' 2>/dev/null"
        )
        parts = status.strip("'").split("/")
        succeeded = parts[0] if len(parts) > 0 else ""
        failed_count = parts[1] if len(parts) > 1 else ""

        if succeeded == "1":
            completed = True
            break
        if failed_count.isdigit() and int(failed_count) > 0:
            break
        time.sleep(10)

    _, logs, _ = run_cmd(
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=400 2>/dev/null",
        timeout=30,
    )
    return completed, logs, failed_count, ""


def cleanup_job(job_name):
    run_cmd(f"kubectl delete job {job_name} -n glitchtip --ignore-not-found=true >/dev/null 2>&1", timeout=20)


def cleanup_pod(pod_name):
    run_cmd(f"kubectl delete pod {pod_name} -n glitchtip --ignore-not-found=true >/dev/null 2>&1", timeout=20)


def get_status_doc():
    rc, out, _ = run_cmd(
        f"kubectl get configmap {STATUS_CONFIGMAP} -n glitchtip "
        f"-o go-template='{{{{ index .data \"{STATUS_KEY}\" }}}}' 2>/dev/null"
    )
    return out.strip("'") if rc == 0 else ""


def get_handoff_doc():
    rc, out, _ = run_cmd(
        f"kubectl get configmap {HANDOFF_CONFIGMAP} -n glitchtip "
        f"-o go-template='{{{{ index .data \"{HANDOFF_KEY}\" }}}}' 2>/dev/null"
    )
    return out.strip("'") if rc == 0 else ""


def get_backup_service_account():
    rc, out, _ = run_cmd(
        "kubectl get cronjob glitchtip-backup -n glitchtip "
        "-o jsonpath='{.spec.jobTemplate.spec.template.spec.serviceAccountName}' 2>/dev/null",
        timeout=20,
    )
    return out.strip("'") if rc == 0 else ""


def get_kubectl_json(cmd, timeout=30):
    rc, out, err = run_cmd(cmd, timeout=timeout)
    if rc != 0:
        return None, err or out or "kubectl command failed"
    try:
        return json.loads(out), ""
    except Exception as e:
        return None, f"Could not parse kubectl JSON output: {e}"


def set_status_doc(text):
    payload = json.dumps({"data": {STATUS_KEY: text}})
    rc, _, err = run_cmd(
        f"kubectl patch configmap {STATUS_CONFIGMAP} -n glitchtip "
        f"--type merge -p {shlex.quote(payload)}",
        timeout=20,
    )
    return rc == 0, err


def set_handoff_doc(text):
    payload = json.dumps({"data": {HANDOFF_KEY: text}})
    rc, _, err = run_cmd(
        f"kubectl patch configmap {HANDOFF_CONFIGMAP} -n glitchtip "
        f"--type merge -p {shlex.quote(payload)}",
        timeout=20,
    )
    return rc == 0, err


def minio_object_exists(minio_pod, bucket, object_key, setup_info):
    rc, _, _ = minio_exec(
        minio_pod,
        f"mc stat local/{bucket}/{object_key} >/dev/null 2>&1",
        setup_info,
    )
    return rc == 0


def minio_get_object_content(minio_pod, bucket, object_key, setup_info):
    rc, out, err = minio_exec(
        minio_pod,
        f"mc cat local/{bucket}/{object_key}",
        setup_info,
    )
    return rc == 0, out, err


def minio_put_object_content(minio_pod, bucket, object_key, content, setup_info):
    return minio_exec(
        minio_pod,
        f"printf '%s' '{content}' | mc pipe local/{bucket}/{object_key} >/dev/null 2>&1",
        setup_info,
    )


def ensure_fileblob_row(pg_pod, blob_path, content, setup_info):
    checksum = content_digest(content)
    size = byte_count(content)
    blob_sql = sql_escape(blob_path)
    checksum_sql = sql_escape(checksum)
    pg_sql(
        pg_pod,
        " ".join([
            f"UPDATE files_fileblob SET checksum = '{checksum_sql}', size = {size}, created = NOW()",
            f"WHERE blob = '{blob_sql}';",
            "INSERT INTO files_fileblob (created, checksum, size, blob)",
            f"SELECT NOW(), '{checksum_sql}', {size}, '{blob_sql}'",
            f"WHERE NOT EXISTS (SELECT 1 FROM files_fileblob WHERE blob = '{blob_sql}');",
        ]),
        setup_info,
    )


def remove_fileblob_rows(pg_pod, blob_path, setup_info):
    pg_sql(
        pg_pod,
        f"DELETE FROM files_fileblob WHERE blob = '{sql_escape(blob_path)}';",
        setup_info,
    )


def wait_for_doc_change(getter, marker, timeout_seconds=60):
    for _ in range(int(timeout_seconds / 5)):
        current = getter()
        if current and marker not in current:
            return current
        time.sleep(5)
    return getter()


def wait_for_status_change(marker, timeout_seconds=60):
    return wait_for_doc_change(get_status_doc, marker, timeout_seconds=timeout_seconds)


def wait_for_handoff_change(marker, timeout_seconds=60):
    return wait_for_doc_change(get_handoff_doc, marker, timeout_seconds=timeout_seconds)


def status_has_success(text):
    lowered = text.lower()
    return any(token in lowered for token in ["status:** success", "status: success", "status=success", "success"])


def status_has_failure(text):
    lowered = text.lower()
    return any(token in lowered for token in ["status:** failed", "status: failed", "status=failed", "failed", "error"])


def first_positive_int(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def handoff_is_machine_readable(text):
    stripped = text.strip()
    if not stripped:
        return False

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return True
    except Exception:
        pass

    kv_lines = [
        line for line in stripped.splitlines()
        if re.search(r"^\s*[A-Za-z0-9_.-]+\s*[:=]\s*.+$", line)
    ]
    return len(kv_lines) >= 3


def handoff_has_success(text):
    lowered = text.lower()
    patterns = [
        r"safe(?:_for_restore| for restore)?[\"'\s:=]+(?:true|yes|1)",
        r"restore(?:_ready| ready)?[\"'\s:=]+(?:true|yes|1)",
        r"result[\"'\s:=]+success",
        r"status[\"'\s:=]+success",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def handoff_has_failure(text):
    lowered = text.lower()
    patterns = [
        r"safe(?:_for_restore| for restore)?[\"'\s:=]+(?:false|no|0)",
        r"restore(?:_ready| ready)?[\"'\s:=]+(?:false|no|0)",
        r"unsafe(?:_for_restore| for restore)?[\"'\s:=]+(?:true|yes|1)",
        r"blocked[\"'\s:=]+(?:true|yes|1)",
        r"result[\"'\s:=]+(?:failed|failure|blocked|unsafe)",
        r"status[\"'\s:=]+(?:failed|failure|blocked|unsafe)",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def handoff_has_run_identity(text):
    lowered = text.lower()
    return bool(re.search(
        r"(?:backup[_ -]?id|run[_ -]?id|latest[_ -]?run|timestamp|completed[_ -]?at|created[_ -]?at)"
        r"[^\n]{0,80}(?:glitchtip-\d{8}_\d{6}|\d{8}_\d{6}|\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}z)",
        lowered,
    ))


def parse_handoff_json(text):
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def handoff_dump_bytes(text):
    parsed = parse_handoff_json(text)
    if parsed:
        pg_dump = parsed.get("pg_dump")
        if isinstance(pg_dump, dict):
            try:
                bytes_value = int(pg_dump.get("bytes", 0))
            except (TypeError, ValueError):
                bytes_value = 0
            if bytes_value > 0:
                return bytes_value
    return first_positive_int(text.lower(), [
        r"(?:pg_dump|dump_bytes|dump size|dump_size|postgresql dump|database dump)[\s\S]{0,120}?(\d+)",
    ])


def handoff_object_count(text):
    parsed = parse_handoff_json(text)
    if parsed:
        attachments = parsed.get("attachments")
        if isinstance(attachments, dict):
            for key in ("mirrored_objects", "verified_objects", "captured_objects", "attachment_objects"):
                try:
                    count = int(attachments.get(key, 0))
                except (TypeError, ValueError):
                    count = 0
                if count > 0:
                    return count
    return first_positive_int(text.lower(), [
        r"(?:attachments?|objects?|minio objects?|bucket objects?|captured_objects|verified_objects|mirrored_objects)"
        r"[\s\S]{0,120}?(\d+)",
    ])


def handoff_has_validation_details(text):
    lowered = text.lower()
    patterns = [
        r"validation(?:_mode)?[\"'\s:=]+(?:exact|set|integrity|verified|matched)",
        r"missing(?:_in_object_storage)?[\"'\s:=]+\d+",
        r"extra(?:_in_object_storage)?[\"'\s:=]+\d+",
        r"integrity(?:_mismatches?)?[\"'\s:=]+\d+",
        r"reason[\"'\s:=]+.+",
        r"blocked_reason[\"'\s:=]+.+",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def validate_handoff_doc(handoff_doc, marker, expect_success, require_artifact_details=False, require_reason=False):
    if not handoff_doc or marker in handoff_doc:
        return False, "Backup run did not update the machine-readable restore handoff surface"
    if not handoff_is_machine_readable(handoff_doc):
        return False, (
            "Restore handoff was updated, but it is not machine-readable enough for downstream tooling "
            "to consume reliably."
        )
    if expect_success and not handoff_has_success(handoff_doc):
        return False, f"Restore handoff does not mark the latest run as safe/successful. Handoff: {handoff_doc[:300]}"
    if not expect_success and not handoff_has_failure(handoff_doc):
        return False, f"Restore handoff does not mark the latest run as blocked/unsafe. Handoff: {handoff_doc[:300]}"
    if require_artifact_details:
        if not handoff_has_run_identity(handoff_doc):
            return False, f"Restore handoff does not identify which run produced the latest backup artifacts. Handoff: {handoff_doc[:300]}"
        if handoff_dump_bytes(handoff_doc) is None:
            return False, f"Restore handoff does not report non-zero dump artifact evidence. Handoff: {handoff_doc[:300]}"
        if handoff_object_count(handoff_doc) is None:
            return False, f"Restore handoff does not report a non-zero attachment capture count. Handoff: {handoff_doc[:300]}"
    if require_reason and not handoff_has_validation_details(handoff_doc):
        return False, (
            "Restore handoff does not explain why the latest run is blocked or how validation was evaluated. "
            f"Handoff: {handoff_doc[:300]}"
        )
    return True, ""


def extract_endpoint_host(setup_info):
    endpoint = setup_info.get("MINIO_ENDPOINT", "http://glitchtip-minio:9000")
    match = re.match(r"https?://([^/:]+)", endpoint)
    return match.group(1) if match else "glitchtip-minio"


def ensure_consistent_dataset(setup_info):
    pg_pod = get_pg_pod()
    minio_pod = get_minio_pod()
    if not pg_pod or not minio_pod:
        return False, "Could not locate PostgreSQL or MinIO pod"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    live_missing = setup_info.get("LIVE_MISSING_BLOB", "")
    live_missing_content = setup_info.get("LIVE_MISSING_CONTENT", "restored-live-baseline-object")
    live_stale = setup_info.get("LIVE_STALE_BLOB", "")
    integrity_target = setup_info.get("INTEGRITY_TARGET_BLOB", "")
    integrity_target_content = setup_info.get("INTEGRITY_TARGET_CONTENT", "")

    if live_missing:
        minio_put_object_content(
            minio_pod,
            bucket,
            live_missing,
            live_missing_content,
            setup_info,
        )
        ensure_fileblob_row(pg_pod, live_missing, live_missing_content, setup_info)

    if live_stale:
        minio_exec(
            minio_pod,
            f"mc rm --force local/{bucket}/{live_stale} >/dev/null 2>&1 || true",
            setup_info,
        )
        remove_fileblob_rows(pg_pod, live_stale, setup_info)

    if integrity_target and integrity_target_content:
        minio_put_object_content(
            minio_pod,
            bucket,
            integrity_target,
            integrity_target_content,
            setup_info,
        )
        ensure_fileblob_row(pg_pod, integrity_target, integrity_target_content, setup_info)

    minio_exec(
        minio_pod,
        f"mc rm --force --recursive local/{bucket}/grader >/dev/null 2>&1 || true",
        setup_info,
    )

    pg_sql(
        pg_pod,
        "DELETE FROM files_fileblob WHERE checksum LIKE 'grader_%' OR blob LIKE 'grader/%';",
        setup_info,
    )

    return True, "Consistent baseline restored"


def restore_live_drift(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return False, message

    minio_pod = get_minio_pod()
    if not minio_pod:
        return False, "Could not locate MinIO pod"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    live_missing = setup_info.get("LIVE_MISSING_BLOB", "")
    live_stale = setup_info.get("LIVE_STALE_BLOB", "")
    live_stale_content = setup_info.get("LIVE_STALE_CONTENT", "live-object-store-leftover")

    if live_missing:
        minio_exec(
            minio_pod,
            f"mc rm --force local/{bucket}/{live_missing} >/dev/null 2>&1 || true",
            setup_info,
        )

    if live_stale:
        minio_put_object_content(
            minio_pod,
            bucket,
            live_stale,
            live_stale_content,
            setup_info,
        )

    return True, "Live drift restored"


def verify_live_incident_untouched(setup_info):
    pg_pod = get_pg_pod()
    minio_pod = get_minio_pod()
    if not pg_pod or not minio_pod:
        return False, "Could not locate PostgreSQL or MinIO pod to verify the live incident state"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    live_missing = setup_info.get("LIVE_MISSING_BLOB", "")
    live_missing_content = setup_info.get("LIVE_MISSING_CONTENT", "")
    live_stale = setup_info.get("LIVE_STALE_BLOB", "")
    live_stale_content = setup_info.get("LIVE_STALE_CONTENT", "")
    integrity_target = setup_info.get("INTEGRITY_TARGET_BLOB", "")
    integrity_target_content = setup_info.get("INTEGRITY_TARGET_CONTENT", "")

    def row_summary(blob_path):
        return pg_sql(
            pg_pod,
            " ".join([
                "SELECT COALESCE(checksum, '') || '|' || COALESCE(size::text, '')",
                "FROM files_fileblob",
                f"WHERE blob = '{sql_escape(blob_path)}'",
                "ORDER BY id DESC LIMIT 1;",
            ]),
            setup_info,
        )

    def row_count(blob_path):
        count = pg_sql(
            pg_pod,
            f"SELECT COUNT(*) FROM files_fileblob WHERE blob = '{sql_escape(blob_path)}';",
            setup_info,
        )
        try:
            return int(count or "0")
        except ValueError:
            return 0

    if live_missing:
        expected_missing_summary = f"{content_digest(live_missing_content)}|{byte_count(live_missing_content)}"
        if row_summary(live_missing) != expected_missing_summary:
            return False, (
                "The live incident was altered: the database metadata for the known missing object "
                f"'{live_missing}' no longer matches the original drill state."
            )
        if minio_object_exists(minio_pod, bucket, live_missing, setup_info):
            return False, (
                "The live incident was altered: the originally missing object "
                f"'{live_missing}' was written back into MinIO instead of being detected by the backup."
            )

    if live_stale:
        if row_count(live_stale) != 0:
            return False, (
                "The live incident was altered: the object-store-only leftover "
                f"'{live_stale}' was added to the database metadata."
            )
        exists, content, _ = minio_get_object_content(minio_pod, bucket, live_stale, setup_info)
        if not exists:
            return False, (
                "The live incident was altered: the object-store-only leftover "
                f"'{live_stale}' was removed from MinIO instead of being reported."
            )
        if content != live_stale_content:
            return False, (
                "The live incident was altered: the object-store-only leftover "
                f"'{live_stale}' no longer has its original drill content."
            )

    if integrity_target:
        expected_integrity_summary = f"{content_digest(integrity_target_content)}|{byte_count(integrity_target_content)}"
        if row_summary(integrity_target) != expected_integrity_summary:
            return False, (
                "The live incident was altered: the database metadata for the known integrity-check "
                f"object '{integrity_target}' no longer matches the original drill state."
            )
        exists, content, _ = minio_get_object_content(minio_pod, bucket, integrity_target, setup_info)
        if not exists:
            return False, (
                "The live incident was altered: the known integrity-check object "
                f"'{integrity_target}' disappeared from MinIO."
            )
        if content != integrity_target_content:
            return False, (
                "The live incident was altered: the known integrity-check object "
                f"'{integrity_target}' was rewritten instead of being backed up and verified as-is."
            )

    return True, (
        "The live incident data still matches the original drill state; the repair did not rewrite "
        "production evidence to force a green run."
    )


def record_text(logs, status_doc, handoff_doc=""):
    return f"{logs}\n{status_doc}\n{handoff_doc}".lower()


def extract_relevant_excerpt(text, expected_path="", window=6, fallback_lines=12):
    lines = text.splitlines()
    lowered_lines = [line.lower() for line in lines]
    expected = expected_path.lower()

    if expected:
        for idx, line in enumerate(lowered_lines):
            if expected in line:
                start = max(0, idx - window)
                end = min(len(lines), idx + window + 1)
                return "\n".join(lines[start:end])

    interesting = [
        idx for idx, line in enumerate(lowered_lines)
        if any(token in line for token in [
            "validation",
            "missing",
            "orphan",
            "extra",
            "integrity",
            "checksum",
            "sha256",
            "hash",
            "corrupt",
            "result:",
            "status configmap",
            "tracked",
            "mirror",
            "backup id",
            "pg_dump",
            "artifact",
        ])
    ]
    if interesting:
        start = max(0, interesting[0] - 2)
        end = min(len(lines), interesting[0] + fallback_lines)
        return "\n".join(lines[start:end])

    return "\n".join(lines[:fallback_lines])


def has_path_with_context_patterns(text, expected_path, patterns, window=10):
    lines = text.splitlines()
    lowered_lines = [line.lower() for line in lines]
    expected = expected_path.lower()

    expected_seen = any(expected in line for line in lowered_lines)
    if not expected_seen:
        return False

    for idx, line in enumerate(lowered_lines):
        if expected in line:
            start = max(0, idx - window)
            end = min(len(lowered_lines), idx + window + 1)
            context = "\n".join(lowered_lines[start:end])
            if any(re.search(pattern, context) for pattern in patterns):
                return True

    whole_text = "\n".join(lowered_lines)
    return any(re.search(pattern, whole_text) for pattern in patterns)


def evidence_for_forward_gap(text, expected_path):
    forward_patterns = [
        r"missing(?: from)? (?:the )?(?:mirrored )?(?:backup|mirror|object storage|minio)",
        r"not found in (?:the )?(?:mirrored )?(?:backup|mirror|object storage|minio)",
        r"(?:files?|objects?|blobs?) referenced in (?:db|database) .*missing",
        r"(?:files?|objects?|blobs?) in (?:db|database) but missing from (?:the )?(?:mirror|backup|object storage)",
        r"(?:db|database).*(?:blob|file|path|record).*(?:missing|not found)",
        r"database references.*missing",
        r"missing_in_object_storage",
        r"lost on restore",
        r"path mismatch",
        r"missing:",
        r"missing files?",
    ]

    return has_path_with_context_patterns(text, expected_path, forward_patterns)


def evidence_for_reverse_gap(text, expected_path):
    reverse_patterns = [
        r"extra (?:object|objects|file|files)",
        r"orphan(?:ed)?",
        r"no matching (?:db|database)(?: record| fileblob)?",
        r"not tracked in (?:db|database)",
        r"not referenced in (?:db|database)",
        r"(?:files?|objects?) in (?:mirror|backup|object storage) but not (?:tracked|referenced) in (?:db|database)",
        r"(?:files?|objects?) in (?:mirror|backup|object storage) but not in (?:db|database)",
        r"(?:files?|objects?) in (?:mirror|backup|object storage) have no matching",
        r"extra_in_object_storage",
        r"leftover",
        r"unexpected (?:object|objects|file|files)",
    ]

    return has_path_with_context_patterns(text, expected_path, reverse_patterns)


def evidence_for_integrity_mismatch(text, expected_path):
    integrity_patterns = [
        r"integrity mismatch",
        r"checksum mismatch",
        r"hash mismatch",
        r"sha256 mismatch",
        r"digest mismatch",
        r"content mismatch",
        r"wrong bytes",
        r"corrupt(?:ed|ion)?",
        r"size mismatch",
        r"mismatch(?:es)? in mirrored content",
        r"integrity_mismatch",
    ]

    return has_path_with_context_patterns(text, expected_path, integrity_patterns)


def verify_minio_access_remains_scoped(setup_info):
    endpoint_host = extract_endpoint_host(setup_info)
    if not endpoint_host:
        endpoint_host = "glitchtip-minio"

    rc, _, _ = run_cmd(
        f"kubectl get networkpolicy {MINIO_ACCESS_POLICY} -n glitchtip "
        "-o jsonpath='{.metadata.name}' 2>/dev/null",
        timeout=20,
    )
    if rc != 0:
        return False, "Scoped MinIO access policy is missing"

    ts = int(time.time())
    probe_name = f"grader-minio-probe-{ts}"
    cleanup_pod(probe_name)

    rc, out, err = run_cmd(
        f"""kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: {probe_name}
  namespace: glitchtip
  labels:
    app: grader-probe
    component: minio-policy-check
spec:
  restartPolicy: Never
  containers:
  - name: probe
    image: docker.io/minio/minio:latest
    imagePullPolicy: IfNotPresent
    command: ["/bin/sh", "-c", "sleep 600"]
EOF""",
        timeout=30,
    )
    if rc != 0:
        return False, f"Could not create unauthorized probe pod: {(err or out)[:200]}"

    try:
        ready = False
        phase = ""
        for _ in range(18):
            rc, phase_out, _ = run_cmd(
                f"kubectl get pod {probe_name} -n glitchtip -o jsonpath='{{.status.phase}}' 2>/dev/null",
                timeout=20,
            )
            phase = phase_out.strip("'")
            if phase == "Running":
                ready = True
                break
            if phase in ("Failed", "Succeeded"):
                return False, f"Unauthorized probe pod exited early with phase {phase}"
            time.sleep(5)

        if not ready:
            return False, f"Unauthorized probe pod did not become ready (last phase: {phase or 'unknown'})"

        rc, out, err = run_cmd(
            f"kubectl exec -n glitchtip {probe_name} -- "
            f"sh -c \"curl -sS -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 6 "
            f"http://{endpoint_host}:9000/minio/health/live\"",
            timeout=20,
        )
    finally:
        cleanup_pod(probe_name)

    status_code = out.strip()
    if rc == 0 and status_code == "200":
        return False, (
            "Unauthorized in-cluster probe can still reach MinIO. "
            "The fix broadened object-store exposure instead of preserving scoped access."
        )

    return True, "MinIO access remains scoped to authorized workloads after the backup fix"


def verify_backup_runtime_permissions_remain_scoped():
    sa_name = get_backup_service_account()
    if not sa_name:
        return False, (
            "Backup CronJob does not declare a dedicated runtime identity. "
            "The job should use a reconciled service account that can publish the run note "
            "and restore handoff without having broad maintenance write access."
        )

    rolebindings, err = get_kubectl_json("kubectl get rolebindings -n glitchtip -o json", timeout=30)
    if rolebindings is None:
        return False, f"Could not inspect rolebindings for backup runtime identity: {err}"

    roles, err = get_kubectl_json("kubectl get roles -n glitchtip -o json", timeout=30)
    if roles is None:
        return False, f"Could not inspect roles for backup runtime identity: {err}"

    clusterrolebindings, err = get_kubectl_json("kubectl get clusterrolebindings -o json", timeout=30)
    if clusterrolebindings is None:
        return False, f"Could not inspect clusterrolebindings for backup runtime identity: {err}"

    clusterroles, err = get_kubectl_json("kubectl get clusterroles -o json", timeout=30)
    if clusterroles is None:
        return False, f"Could not inspect clusterroles for backup runtime identity: {err}"

    role_map = {item["metadata"]["name"]: item.get("rules", []) for item in roles.get("items", [])}
    clusterrole_map = {item["metadata"]["name"]: item.get("rules", []) for item in clusterroles.get("items", [])}

    def subject_matches(subject):
        return (
            subject.get("kind") == "ServiceAccount"
            and subject.get("name") == sa_name
            and subject.get("namespace", "glitchtip") == "glitchtip"
        )

    bound_rules = []
    for binding in rolebindings.get("items", []):
        subjects = binding.get("subjects") or []
        if not any(subject_matches(subject) for subject in subjects):
            continue
        role_ref = binding.get("roleRef") or {}
        if role_ref.get("kind") == "Role":
            bound_rules.extend(role_map.get(role_ref.get("name", ""), []))
        elif role_ref.get("kind") == "ClusterRole":
            bound_rules.extend(clusterrole_map.get(role_ref.get("name", ""), []))

    for binding in clusterrolebindings.get("items", []):
        subjects = binding.get("subjects") or []
        if not any(subject_matches(subject) for subject in subjects):
            continue
        role_ref = binding.get("roleRef") or {}
        if role_ref.get("kind") == "ClusterRole":
            bound_rules.extend(clusterrole_map.get(role_ref.get("name", ""), []))

    def rule_allows(rule, api_group, resource, verbs, resource_name=None):
        api_groups = rule.get("apiGroups") or [""]
        resources = rule.get("resources") or []
        rule_verbs = rule.get("verbs") or []
        if api_group not in api_groups and "*" not in api_groups:
            return False
        if resource not in resources and "*" not in resources:
            return False
        if not any(verb in rule_verbs or "*" in rule_verbs for verb in verbs):
            return False
        resource_names = rule.get("resourceNames")
        if resource_name is None:
            return True
        if resource_names is None or "*" in resource_names:
            return True
        return resource_name in resource_names

    def rule_is_broad_config_writer(rule):
        if not rule_allows(rule, "", "configmaps", ["patch", "update"]):
            return False
        resource_names = rule.get("resourceNames")
        if resource_names is None or "*" in resource_names:
            return True
        return not set(resource_names).issubset(RUNTIME_CONFIGMAP_TARGETS)

    def rule_can_patch_backup_cronjob(rule):
        return rule_allows(rule, "batch", "cronjobs", ["patch", "update"], "glitchtip-backup")

    can_patch_status = any(
        rule_allows(rule, "", "configmaps", ["patch", "update"], STATUS_CONFIGMAP)
        for rule in bound_rules
    )
    can_patch_handoff = any(
        rule_allows(rule, "", "configmaps", ["patch", "update"], HANDOFF_CONFIGMAP)
        for rule in bound_rules
    )
    has_broad_config_access = any(rule_is_broad_config_writer(rule) for rule in bound_rules)
    can_patch_backup_cronjob = any(rule_can_patch_backup_cronjob(rule) for rule in bound_rules)

    if not can_patch_status:
        return False, (
            f"Backup runtime identity '{sa_name}' cannot publish the run-status note. "
            "It needs narrow write access to the status ConfigMap."
        )
    if not can_patch_handoff:
        return False, (
            f"Backup runtime identity '{sa_name}' cannot refresh the machine-readable restore handoff. "
            "It needs narrow write access to the restore-handoff ConfigMap."
        )

    if has_broad_config_access or can_patch_backup_cronjob:
        return False, (
            f"Backup runtime identity '{sa_name}' is too broad. "
            "It can rewrite backup configuration instead of only publishing the run note and restore handoff."
        )

    return True, (
        f"Backup runtime identity '{sa_name}' can publish the run note and restore handoff "
        "without having broad write access to the backup configuration."
    )


def check_clean_run_proves_real_backup_artifacts(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    ts = int(time.time())
    marker = f"GRADER-ARTIFACT-{ts}"
    set_status_doc(marker)
    set_handoff_doc(marker)

    job_name = f"grader-artifact-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    handoff_doc = wait_for_handoff_change(marker)
    cleanup_job(job_name)

    job_failed = failed.isdigit() and int(failed) > 0
    logs_lower = logs.lower()
    status_lower = status_doc.lower()

    if err:
        return 0.0, err
    if not completed or job_failed:
        return 0.0, f"Clean artifact-evidence run did not succeed. Logs:\n{extract_relevant_excerpt(logs)}"
    if not status_doc or marker in status_doc:
        return 0.0, "Clean artifact-evidence run did not update the persistent status surface"
    if not status_has_success(status_doc):
        return 0.0, f"Clean artifact-evidence status does not indicate success. Status: {status_doc[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        handoff_doc,
        marker,
        expect_success=True,
        require_artifact_details=True,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    has_dump_log = "glitchtip_db.dump" in logs_lower
    has_manifest_log = "backup_manifest.txt" in logs_lower or "recording backup metadata" in logs_lower
    run_marker_ok = bool(re.search(
        r"(?:backup id|backup_id|run id|run_id|latest run|latest recorded run)[^\n]*\S",
        status_lower,
    ))
    dump_status_ok = bool(re.search(
        r"(?:postgresql dump|database dump|pg_dump)[^\n]*(?:success|ok|completed|produced|\b\d+\s*bytes\b)",
        status_lower,
    ))
    dump_size = first_positive_int(status_lower, [
        r"(?:pg_dump(?:[_ ](?:size|bytes))?|pg dump(?: size| bytes)?|postgresql dump|database dump(?: size)?)[^0-9\n]{0,60}(\d+)\s*bytes?",
        r"(?:pg_dump_bytes|pg dump bytes)[^0-9\n]{0,20}(\d+)",
    ])
    object_count = first_positive_int(status_lower, [
        r"(?:attachment capture|attachments? captured|attachments? mirrored)[^0-9\n]{0,60}(\d+)",
        r"(?:mirrored object count|mirrored_objects|mirrored objects?)[^0-9\n]{0,60}(\d+)",
        r"(?:storage object count|storage objects? mirrored)[^0-9\n]{0,60}(\d+)",
        r"(?:minio objects? mirrored|bucket objects?|objects? mirrored from bucket)[^0-9\n]{0,60}(\d+)",
        r"(?:attachment objects?|captured objects?)[^0-9\n]{0,60}(\d+)",
    ])

    if not has_dump_log:
        return 0.0, "Clean run did not show evidence of a real PostgreSQL dump artifact in the job logs"
    if not has_manifest_log:
        return 0.0, "Clean run did not show evidence of recorded backup metadata in the job logs"
    if not run_marker_ok:
        return 0.0, f"Clean run status does not identify which clean run produced the artifacts. Status: {status_doc[:300]}"
    if not dump_status_ok and dump_size is None:
        return 0.0, f"Clean run status does not report a successful database dump. Status: {status_doc[:300]}"
    if object_count is None:
        return 0.0, f"Clean run status does not report a non-zero attachment capture size. Status: {status_doc[:300]}"

    return 1.0, (
        "Clean run proves a real dump and object capture were produced, and it refreshes "
        "the machine-readable restore handoff for downstream recovery tooling"
    )


def check_pipeline_handles_clean_and_drift_runs(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    ts = int(time.time())
    clean_marker = f"GRADER-CLEAN-{ts}"
    set_status_doc(clean_marker)
    set_handoff_doc(clean_marker)

    clean_job = f"grader-clean-{ts}"
    clean_completed, clean_logs, clean_failed, clean_err = trigger_backup_job(clean_job)
    clean_status = wait_for_status_change(clean_marker)
    clean_handoff = wait_for_handoff_change(clean_marker)
    cleanup_job(clean_job)

    clean_failed_job = clean_failed.isdigit() and int(clean_failed) > 0
    if clean_err:
        return 0.0, clean_err
    if not clean_completed or clean_failed_job:
        return 0.0, f"Clean backup run did not succeed. Logs:\n{extract_relevant_excerpt(clean_logs)}"
    if not clean_status or clean_marker in clean_status:
        return 0.0, "Clean backup run did not update the persistent status surface"
    if not status_has_success(clean_status):
        return 0.0, f"Clean backup status does not indicate success. Status: {clean_status[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        clean_handoff,
        clean_marker,
        expect_success=True,
        require_artifact_details=True,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    ok, message = restore_live_drift(setup_info)
    if not ok:
        return 0.0, message

    drift_marker = f"GRADER-DRIFT-{ts}"
    set_status_doc(drift_marker)
    set_handoff_doc(drift_marker)

    drift_job = f"grader-drift-{ts}"
    drift_completed, drift_logs, drift_failed, drift_err = trigger_backup_job(drift_job)
    drift_status = wait_for_status_change(drift_marker)
    drift_handoff = wait_for_handoff_change(drift_marker)
    cleanup_job(drift_job)

    ensure_consistent_dataset(setup_info)

    drift_failed_job = drift_failed.isdigit() and int(drift_failed) > 0
    drift_text = record_text(drift_logs, drift_status, drift_handoff)
    expected_missing = setup_info.get("LIVE_MISSING_BLOB", "")
    expected_stale = setup_info.get("LIVE_STALE_BLOB", "")

    if drift_err:
        return 0.0, drift_err
    if not drift_failed_job:
        return 0.0, f"Built-in live drift did not fail closed. Logs:\n{extract_relevant_excerpt(drift_logs, expected_missing)}"
    if not drift_status or drift_marker in drift_status:
        return 0.0, "Dirty backup run did not update the persistent status surface"
    if not status_has_failure(drift_status):
        return 0.0, f"Dirty backup status does not indicate failure. Status: {drift_status[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        drift_handoff,
        drift_marker,
        expect_success=False,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    if not evidence_for_forward_gap(drift_text, expected_missing):
        return 0.0, (
            "Dirty run did not report the missing object side of the drift. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(drift_text, expected_missing)}"
        )
    if not evidence_for_reverse_gap(drift_text, expected_stale):
        return 0.0, (
            "Dirty run did not report the extra object side of the drift. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(drift_text, expected_stale)}"
        )
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback
    scoped_ok, scoped_feedback = verify_minio_access_remains_scoped(setup_info)
    if not scoped_ok:
        return 0.0, scoped_feedback

    return 1.0, (
        "Pipeline succeeds on clean data, fails on live drift, keeps the restore handoff truthful, "
        "reports both mismatch directions, and keeps MinIO access scoped"
    )


def check_validation_catches_forward_gap(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    pg_pod = get_pg_pod()
    if not pg_pod:
        return 0.0, "No PostgreSQL pod found"

    ts = int(time.time())
    orphan_key = f"grader/forward/missing_{ts}.dmp"
    pg_sql(
        pg_pod,
        f"INSERT INTO files_fileblob (created, checksum, size, blob) "
        f"VALUES (NOW(), 'grader_fwd_{ts}', 1024, '{orphan_key}') "
        f"ON CONFLICT (checksum) DO NOTHING;",
        setup_info,
    )

    marker = f"GRADER-FWD-{ts}"
    set_status_doc(marker)
    set_handoff_doc(marker)

    job_name = f"grader-forward-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    handoff_doc = wait_for_handoff_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc, handoff_doc)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Forward-gap validation did not fail closed. Logs:\n{extract_relevant_excerpt(logs, orphan_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Forward-gap run did not publish a failed status. Status: {status_doc[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        handoff_doc,
        marker,
        expect_success=False,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    if not evidence_for_forward_gap(text, orphan_key):
        return 0.0, (
            "Forward-gap run did not report the missing object evidence. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(text, orphan_key)}"
        )
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    return 1.0, "Validation fails closed and reports a database record with no matching object"


def check_validation_catches_reverse_gap(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    minio_pod = get_minio_pod()
    if not minio_pod:
        return 0.0, "No MinIO pod found"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    ts = int(time.time())
    extra_key = f"grader/reverse/extra_{ts}.bin"
    minio_exec(
        minio_pod,
        f"echo 'grader-extra-object-{ts}' | mc pipe local/{bucket}/{extra_key} >/dev/null 2>&1",
        setup_info,
    )

    marker = f"GRADER-REV-{ts}"
    set_status_doc(marker)
    set_handoff_doc(marker)

    job_name = f"grader-reverse-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    handoff_doc = wait_for_handoff_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc, handoff_doc)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Reverse-gap validation did not fail closed. Logs:\n{extract_relevant_excerpt(logs, extra_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Reverse-gap run did not publish a failed status. Status: {status_doc[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        handoff_doc,
        marker,
        expect_success=False,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    if not evidence_for_reverse_gap(text, extra_key):
        return 0.0, (
            "Reverse-gap run did not report the extra object evidence. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(text, extra_key)}"
        )
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    return 1.0, "Validation fails closed and reports an object with no matching database record"


def check_validation_catches_balanced_set_drift(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    pg_pod = get_pg_pod()
    minio_pod = get_minio_pod()
    if not pg_pod or not minio_pod:
        return 0.0, "Could not locate PostgreSQL or MinIO pod"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    ts = int(time.time())
    missing_key = f"grader/balanced/missing_{ts}.dmp"
    extra_key = f"grader/balanced/extra_{ts}.bin"

    pg_sql(
        pg_pod,
        f"INSERT INTO files_fileblob (created, checksum, size, blob) "
        f"VALUES (NOW(), 'grader_bal_{ts}', 1024, '{missing_key}') "
        f"ON CONFLICT (checksum) DO NOTHING;",
        setup_info,
    )
    minio_exec(
        minio_pod,
        f"echo 'grader-balanced-extra-{ts}' | mc pipe local/{bucket}/{extra_key} >/dev/null 2>&1",
        setup_info,
    )

    marker = f"GRADER-BAL-{ts}"
    set_status_doc(marker)
    set_handoff_doc(marker)

    job_name = f"grader-balanced-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    handoff_doc = wait_for_handoff_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc, handoff_doc)
    missing_reported = evidence_for_forward_gap(text, missing_key)
    extra_reported = evidence_for_reverse_gap(text, extra_key)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Balanced drift did not fail closed. Logs:\n{extract_relevant_excerpt(logs, missing_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Balanced drift run did not publish a failed status. Status: {status_doc[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        handoff_doc,
        marker,
        expect_success=False,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    if not (missing_reported and extra_reported):
        return 0.0, (
            "Balanced drift was not reported as both missing and extra set drift. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(text, missing_key)}"
        )
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    return 1.0, "Validation catches equal-count set drift instead of relying on simple totals"


def check_validation_catches_integrity_drift(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    minio_pod = get_minio_pod()
    if not minio_pod:
        return 0.0, "No MinIO pod found"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    target_key = setup_info.get("INTEGRITY_TARGET_BLOB", "")
    corrupted_content = setup_info.get("INTEGRITY_CORRUPTED_CONTENT", "")
    if not target_key or not corrupted_content:
        return 0.0, "Integrity drift fixtures are missing from setup"

    minio_put_object_content(
        minio_pod,
        bucket,
        target_key,
        corrupted_content,
        setup_info,
    )

    ts = int(time.time())
    marker = f"GRADER-INT-{ts}"
    set_status_doc(marker)
    set_handoff_doc(marker)

    job_name = f"grader-integrity-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    handoff_doc = wait_for_handoff_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc, handoff_doc)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Integrity drift did not fail closed. Logs:\n{extract_relevant_excerpt(logs, target_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Integrity drift run did not publish a failed status. Status: {status_doc[:300]}"
    handoff_ok, handoff_feedback = validate_handoff_doc(
        handoff_doc,
        marker,
        expect_success=False,
        require_reason=True,
    )
    if not handoff_ok:
        return 0.0, handoff_feedback
    if not evidence_for_integrity_mismatch(text, target_key):
        return 0.0, (
            "Integrity drift run did not report corrupted mirrored content. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(text, target_key)}"
        )
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    return 1.0, "Validation fails closed when an object key exists but its mirrored bytes do not match the database metadata"


def check_validation_catches_all_drift_modes(setup_info):
    scenarios = [
        ("forward gap", check_validation_catches_forward_gap),
        ("reverse gap", check_validation_catches_reverse_gap),
        ("balanced set drift", check_validation_catches_balanced_set_drift),
        ("integrity drift", check_validation_catches_integrity_drift),
    ]

    feedback_parts = []
    for label, check_fn in scenarios:
        score, feedback = check_fn(setup_info)
        feedback_parts.append(f"{label}: {feedback}")
        if score <= 0:
            return 0.0, (
                "Validation does not handle every required drift mode. "
                + " | ".join(feedback_parts)
            )

    return 1.0, (
        "Validation fails closed and reports forward gaps, reverse gaps, "
        "equal-count set drift, and wrong-bytes integrity drift within the same backup design"
    )


def grade(*args, **kwargs) -> GradingResult:
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    checks = {
        "pipeline_handles_clean_and_drift_runs": check_pipeline_handles_clean_and_drift_runs,
        "clean_run_proves_real_backup_artifacts": check_clean_run_proves_real_backup_artifacts,
        "validation_catches_all_drift_modes": check_validation_catches_all_drift_modes,
    }
    weight = 1.0 / len(checks)

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 240s durability window...")
    time.sleep(240)

    incident_ok, incident_feedback = verify_live_incident_untouched(setup_info)
    if not incident_ok:
        subscores = {name: 0.0 for name in checks}
        weights = {name: weight for name in checks}
        feedback_str = (
            "[live_incident_untouched] FAIL: "
            + incident_feedback
            + "\nThe task explicitly forbids rewriting the current attachment bucket or database rows "
              "just to force a green drill."
        )
        print(f"[grader] live_incident_untouched: 0.0 — {incident_feedback}")
        print("\n[grader] Final score: 0.0000")
        return GradingResult(
            score=0.0,
            subscores=subscores,
            weights=weights,
            feedback=feedback_str,
        )

    subscores = {}
    weights = {}
    feedback_parts = []

    for name, check_fn in checks.items():
        try:
            score, feedback = check_fn(setup_info)
        except Exception as e:
            score, feedback = 0.0, f"Exception: {e}"

        subscores[name] = score
        weights[name] = weight
        feedback_parts.append(f"[{name}] {'PASS' if score > 0 else 'FAIL'}: {feedback}")
        print(f"[grader] {name}: {score} — {feedback}")

    total_score = sum(subscores[k] * weights[k] for k in subscores)
    feedback_str = "\n".join(feedback_parts)

    print(f"\n[grader] Final score: {total_score:.4f}")
    return GradingResult(
        score=total_score,
        subscores=subscores,
        weights=weights,
        feedback=feedback_str,
    )


if __name__ == "__main__":
    result = grade()
    print(f"\nScore: {result.score}")
    print(f"Subscores: {result.subscores}")
