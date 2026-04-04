#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap.

The task is no longer "does the script mention MinIO?".
It is "does the backup pipeline behave correctly on both clean and dirty data?".

Objectives:
1. pipeline_handles_clean_and_drift_runs
   - A clean run must succeed and update the persistent status surface.
   - The built-in live drift scenario must fail, report both sides of the drift,
     and preserve scoped MinIO access.
2. clean_run_proves_real_backup_artifacts
   - A clean run must show evidence that a real dump and object capture were
     produced, not just that validation passed.
3. validation_catches_forward_gap
   - A database record with no matching object must fail closed and be reported.
4. validation_catches_reverse_gap
   - An object with no matching database record must fail closed and be reported.
5. validation_catches_balanced_set_drift
   - Equal counts but mismatched sets must still fail closed and be reported.
"""

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


def get_backup_service_account():
    rc, out, _ = run_cmd(
        "kubectl get cronjob glitchtip-backup -n glitchtip "
        "-o jsonpath='{.spec.jobTemplate.spec.template.spec.serviceAccountName}' 2>/dev/null",
        timeout=20,
    )
    return out.strip("'") if rc == 0 else ""


def set_status_doc(text):
    payload = json.dumps({"data": {STATUS_KEY: text}})
    rc, _, err = run_cmd(
        f"kubectl patch configmap {STATUS_CONFIGMAP} -n glitchtip "
        f"--type merge -p {shlex.quote(payload)}",
        timeout=20,
    )
    return rc == 0, err


def wait_for_status_change(marker, timeout_seconds=60):
    for _ in range(int(timeout_seconds / 5)):
        current = get_status_doc()
        if current and marker not in current:
            return current
        time.sleep(5)
    return get_status_doc()


def status_has_success(text):
    lowered = text.lower()
    return any(token in lowered for token in ["status:** success", "status: success", "status=success", "success"])


def status_has_failure(text):
    lowered = text.lower()
    return any(token in lowered for token in ["status:** failed", "status: failed", "status=failed", "failed", "error"])


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
    live_stale = setup_info.get("LIVE_STALE_BLOB", "")

    if live_missing:
        minio_exec(
            minio_pod,
            f"echo 'restored-live-baseline-object' | mc pipe local/{bucket}/{live_missing} >/dev/null 2>&1",
            setup_info,
        )

    if live_stale:
        minio_exec(
            minio_pod,
            f"mc rm --force local/{bucket}/{live_stale} >/dev/null 2>&1 || true",
            setup_info,
        )

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
    pg_pod = get_pg_pod()
    minio_pod = get_minio_pod()
    if not pg_pod or not minio_pod:
        return False, "Could not locate PostgreSQL or MinIO pod"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    live_missing = setup_info.get("LIVE_MISSING_BLOB", "")
    live_stale = setup_info.get("LIVE_STALE_BLOB", "")

    if live_missing:
        minio_exec(
            minio_pod,
            f"mc rm --force local/{bucket}/{live_missing} >/dev/null 2>&1 || true",
            setup_info,
        )

    if live_stale:
        minio_exec(
            minio_pod,
            f"echo 'live-object-store-leftover' | mc pipe local/{bucket}/{live_stale} >/dev/null 2>&1",
            setup_info,
        )

    return True, "Live drift restored"


def record_text(logs, status_doc):
    return f"{logs}\n{status_doc}".lower()


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
            "The job should use a reconciled service account that can publish status without "
            "having broad maintenance write access."
        )

    subject = f"system:serviceaccount:glitchtip:{sa_name}"

    def can_i(verb, resource, resource_name):
        rc, out, _ = run_cmd(
            f"kubectl auth can-i --as={subject} {verb} {resource} "
            f"--resource-name={resource_name} -n glitchtip",
            timeout=20,
        )
        return rc == 0 and out.strip().lower() == "yes"

    can_get_status = can_i("get", "configmaps", STATUS_CONFIGMAP)
    can_patch_status = can_i("patch", "configmaps", STATUS_CONFIGMAP)
    can_update_status = can_i("update", "configmaps", STATUS_CONFIGMAP)
    can_patch_script = can_i("patch", "configmaps", "glitchtip-backup-script")
    can_patch_original = can_i("patch", "configmaps", "glitchtip-backup-script-original")
    can_patch_runtime_baseline = can_i("patch", "configmaps", "glitchtip-backup-runtime-original")
    can_patch_backup_cronjob = can_i("patch", "cronjobs.batch", "glitchtip-backup")

    if not can_get_status or not (can_patch_status or can_update_status):
        return False, (
            f"Backup runtime identity '{sa_name}' cannot publish the run-status note. "
            "It needs narrow write access to the status ConfigMap."
        )

    if any([can_patch_script, can_patch_original, can_patch_runtime_baseline, can_patch_backup_cronjob]):
        return False, (
            f"Backup runtime identity '{sa_name}' is too broad. "
            "It can rewrite backup configuration instead of only publishing status."
        )

    return True, (
        f"Backup runtime identity '{sa_name}' can publish status without having broad "
        "write access to the backup configuration."
    )


def check_clean_run_proves_real_backup_artifacts(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    ts = int(time.time())
    marker = f"GRADER-ARTIFACT-{ts}"
    set_status_doc(marker)

    job_name = f"grader-artifact-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
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
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    has_dump_log = "glitchtip_db.dump" in logs_lower
    has_manifest_log = "backup_manifest.txt" in logs_lower or "recording backup metadata" in logs_lower
    backup_id_match = re.search(r"(?:backup id|backup_id)[^a-z0-9-]*(glitchtip-\d{8}_\d{6})", status_lower)
    dump_status_ok = re.search(r"(?:postgresql dump|pg_dump|database dump)[^\n]*(?:success|ok|completed|\b\d+\s*bytes\b)", status_lower)
    dump_size_match = re.search(r"(?:pg_dump size|database dump size|postgresql dump)[^0-9]*(\d+)\s*bytes", status_lower)
    db_count_match = re.search(r"(?:database fileblob count|pg_fileblobs)[^0-9]*(\d+)", status_lower)
    object_count_match = re.search(r"(?:mirrored object count|mirrored_objects)[^0-9]*(\d+)", status_lower)

    if not has_dump_log:
        return 0.0, "Clean run did not show evidence of a real PostgreSQL dump artifact in the job logs"
    if not has_manifest_log:
        return 0.0, "Clean run did not show evidence of recorded backup metadata in the job logs"
    if not backup_id_match:
        return 0.0, f"Clean run status does not include a backup identifier. Status: {status_doc[:300]}"
    if not dump_status_ok and not dump_size_match:
        return 0.0, f"Clean run status does not report a successful database dump. Status: {status_doc[:300]}"
    if not db_count_match or int(db_count_match.group(1)) <= 0:
        return 0.0, f"Clean run status does not report a non-zero database snapshot size. Status: {status_doc[:300]}"
    if not object_count_match or int(object_count_match.group(1)) <= 0:
        return 0.0, f"Clean run status does not report a non-zero attachment capture size. Status: {status_doc[:300]}"

    return 1.0, "Clean run proves a real dump and object capture were produced, not just a green validation result"


def check_pipeline_handles_clean_and_drift_runs(setup_info):
    ok, message = ensure_consistent_dataset(setup_info)
    if not ok:
        return 0.0, message

    ts = int(time.time())
    clean_marker = f"GRADER-CLEAN-{ts}"
    set_status_doc(clean_marker)

    clean_job = f"grader-clean-{ts}"
    clean_completed, clean_logs, clean_failed, clean_err = trigger_backup_job(clean_job)
    clean_status = wait_for_status_change(clean_marker)
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
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    ok, message = restore_live_drift(setup_info)
    if not ok:
        return 0.0, message

    drift_marker = f"GRADER-DRIFT-{ts}"
    set_status_doc(drift_marker)

    drift_job = f"grader-drift-{ts}"
    drift_completed, drift_logs, drift_failed, drift_err = trigger_backup_job(drift_job)
    drift_status = wait_for_status_change(drift_marker)
    cleanup_job(drift_job)

    ensure_consistent_dataset(setup_info)

    drift_failed_job = drift_failed.isdigit() and int(drift_failed) > 0
    drift_text = record_text(drift_logs, drift_status)
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

    return 1.0, "Pipeline succeeds on clean data, fails on live drift, reports both mismatch directions, and keeps MinIO access scoped"


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

    job_name = f"grader-forward-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Forward-gap validation did not fail closed. Logs:\n{extract_relevant_excerpt(logs, orphan_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Forward-gap run did not publish a failed status. Status: {status_doc[:300]}"
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

    job_name = f"grader-reverse-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Reverse-gap validation did not fail closed. Logs:\n{extract_relevant_excerpt(logs, extra_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Reverse-gap run did not publish a failed status. Status: {status_doc[:300]}"
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

    job_name = f"grader-balanced-{ts}"
    completed, logs, failed, err = trigger_backup_job(job_name)
    status_doc = wait_for_status_change(marker)
    cleanup_job(job_name)
    ensure_consistent_dataset(setup_info)

    job_failed = failed.isdigit() and int(failed) > 0
    text = record_text(logs, status_doc)
    missing_reported = evidence_for_forward_gap(text, missing_key)
    extra_reported = evidence_for_reverse_gap(text, extra_key)

    if err:
        return 0.0, err
    if not job_failed:
        return 0.0, f"Balanced drift did not fail closed. Logs:\n{extract_relevant_excerpt(logs, missing_key)}"
    if not status_doc or marker in status_doc or not status_has_failure(status_doc):
        return 0.0, f"Balanced drift run did not publish a failed status. Status: {status_doc[:300]}"
    if not (missing_reported and extra_reported):
        return 0.0, (
            "Balanced drift was not reported as both missing and extra set drift. "
            f"Relevant excerpt:\n{extract_relevant_excerpt(text, missing_key)}"
        )
    runtime_ok, runtime_feedback = verify_backup_runtime_permissions_remain_scoped()
    if not runtime_ok:
        return 0.0, runtime_feedback

    return 1.0, "Validation catches equal-count set drift instead of relying on simple totals"


def grade(*args, **kwargs) -> GradingResult:
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 240s durability window...")
    time.sleep(240)

    checks = {
        "pipeline_handles_clean_and_drift_runs": check_pipeline_handles_clean_and_drift_runs,
        "clean_run_proves_real_backup_artifacts": check_clean_run_proves_real_backup_artifacts,
        "validation_catches_forward_gap": check_validation_catches_forward_gap,
        "validation_catches_reverse_gap": check_validation_catches_reverse_gap,
        "validation_catches_balanced_set_drift": check_validation_catches_balanced_set_drift,
    }

    weight = 1.0 / len(checks)
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
