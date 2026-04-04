#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap task.

ALL checks are STRICT FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. backup_script_has_minio — Backup ConfigMap has MinIO backup commands
2. backup_handles_concurrent_writes — Backup handles data injected DURING the backup run
3. validation_catches_orphans — Backup detects PG records with no MinIO objects
4. backup_notifies_on_failure — Backup script reports failure status to a discoverable location
"""

import json
import os
import re
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
        timeout=15,
    )
    return out.strip()


def minio_count(minio_pod, setup_info):
    ak = setup_info.get("MINIO_ACCESS_KEY", "")
    sk = setup_info.get("MINIO_SECRET_KEY", "")
    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    rc, out, _ = run_cmd(
        f"kubectl exec -n glitchtip {minio_pod} -- "
        f"sh -c \"mc alias set local http://localhost:9000 '{ak}' '{sk}' "
        f">/dev/null 2>&1; mc ls --recursive local/{bucket}/ 2>/dev/null | wc -l\"",
        timeout=15,
    )
    return int(out.strip()) if out.strip().isdigit() else 0


def trigger_backup_job(job_name, wait_seconds=300):
    """Trigger a backup Job from the CronJob and wait for completion."""
    rc, _, stderr = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=15,
    )
    if rc != 0:
        return False, f"Could not create job: {stderr[:200]}", ""

    completed = False
    failed_count = ""
    for i in range(int(wait_seconds / 10)):
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

    rc, logs, _ = run_cmd(
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=300 2>/dev/null"
    )
    return completed, logs, failed_count


# ─────────────────────────────────────────────
# CHECK 1: backup_script_has_minio
# ─────────────────────────────────────────────
def check_backup_script_has_minio(setup_info):
    """
    Check that backup ConfigMap contains MinIO backup commands.
    Accepts: mc, s3cmd, aws s3, boto3, curl with S3 — any valid approach.
    """
    rc, cm_json, _ = run_cmd(
        "kubectl get configmap glitchtip-backup-script -n glitchtip -o json 2>/dev/null"
    )
    if rc != 0 or not cm_json:
        return 0.0, "Could not read backup script ConfigMap"

    try:
        cm = json.loads(cm_json)
        all_scripts = " ".join(cm.get("data", {}).values())
    except (json.JSONDecodeError, AttributeError):
        return 0.0, "Failed to parse ConfigMap"

    script_lower = all_scripts.lower()

    has_backup_tool = (
        "mc " in script_lower or "mc mirror" in script_lower or
        "aws s3" in script_lower or "s3cmd" in script_lower or
        "boto3" in script_lower or "s3.client" in script_lower or
        "${mc}" in script_lower or '"${mc}"' in script_lower or
        "minio_backup" in script_lower
    )
    has_ref = "minio" in script_lower or "s3" in script_lower or "attachment" in script_lower

    if has_backup_tool and has_ref:
        return 1.0, "Backup script includes MinIO/S3 backup commands"
    elif has_ref:
        return 0.0, "Script references MinIO but no backup tool found"
    else:
        return 0.0, "No MinIO backup step found in backup scripts"


# ─────────────────────────────────────────────
# CHECK 2: backup_handles_concurrent_writes
# ─────────────────────────────────────────────
def check_backup_handles_concurrent_writes(setup_info):
    """
    FUNCTIONAL E2E: Start a backup Job, then DURING the backup inject new
    MinIO objects and PG records. After the Job completes, verify:
    - The backup didn't crash/corrupt from concurrent data
    - The Job still exited 0
    - Logs show both PG and MinIO backup evidence

    Agents never design for concurrent writes. Their scripts assume static data.
    If the backup crashes or exits non-zero due to the injected data, this fails.
    """
    pg_pod = get_pg_pod()
    minio_pod = get_minio_pod()
    if not pg_pod or not minio_pod:
        return 0.0, f"Missing pods: pg={bool(pg_pod)}, minio={bool(minio_pod)}"

    job_name = f"grader-concurrent-{int(time.time())}"

    # Start the backup Job
    rc, _, stderr = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=15,
    )
    if rc != 0:
        return 0.0, f"Could not create backup job: {stderr[:200]}"

    # Wait for the Job pod to be running (not just created)
    for i in range(30):
        rc, phase, _ = run_cmd(
            f"kubectl get pods -n glitchtip -l job-name={job_name} "
            f"-o jsonpath='{{.items[0].status.phase}}' 2>/dev/null"
        )
        if phase.strip("'") in ("Running", "Succeeded"):
            break
        time.sleep(5)

    # NOW inject concurrent data while backup is running
    ts = int(time.time())
    ak = setup_info.get("MINIO_ACCESS_KEY", "")
    sk = setup_info.get("MINIO_SECRET_KEY", "")
    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")

    # Inject 3 new MinIO objects
    for i in range(3):
        run_cmd(
            f"kubectl exec -n glitchtip {minio_pod} -- "
            f"sh -c \"echo 'concurrent-write-{ts}-{i}' | mc pipe local/{bucket}/concurrent/obj_{ts}_{i}.dat 2>/dev/null\"",
            timeout=10,
        )

    # Inject 3 new PG records
    for i in range(3):
        pg_sql(pg_pod,
            f"INSERT INTO files_fileblob (created, checksum, size, blob) "
            f"VALUES (NOW(), 'concurrent_{ts}_{i}', 512, 'concurrent/obj_{ts}_{i}.dat') "
            f"ON CONFLICT (checksum) DO NOTHING;",
            setup_info,
        )

    # Wait for the Job to finish (up to 5 min)
    completed = False
    for i in range(30):
        rc, status, _ = run_cmd(
            f"kubectl get job {job_name} -n glitchtip "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}' 2>/dev/null"
        )
        parts = status.strip("'").split("/")
        succeeded = parts[0] if len(parts) > 0 else ""
        failed = parts[1] if len(parts) > 1 else ""

        if succeeded == "1":
            completed = True
            break
        if failed.isdigit() and int(failed) > 0:
            break
        time.sleep(10)

    # Get logs
    rc, logs, _ = run_cmd(
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=300 2>/dev/null"
    )

    if not completed:
        return 0.0, f"Backup crashed during concurrent writes (did not exit 0). Logs: {logs[:300]}"

    has_pg = any(w in logs.lower() for w in ["pg_dump", "postgresql", "dump complete", "glitchtip_db.dump"])
    has_minio = any(w in logs.lower() for w in ["mirror", "mc mirror", "minio backup", "objects mirrored", "attachments"])

    if has_pg and has_minio:
        return 1.0, "Backup survived concurrent writes — completed with PG and MinIO evidence"
    elif has_pg:
        return 0.0, f"Backup completed but no MinIO evidence after concurrent injection. Logs: {logs[:200]}"
    else:
        return 0.0, f"Backup completed but missing evidence. Logs: {logs[:200]}"


# ─────────────────────────────────────────────
# CHECK 3: validation_catches_orphans
# ─────────────────────────────────────────────
def check_validation_catches_orphans(setup_info):
    """
    FUNCTIONAL E2E: Insert an orphan PG record (metadata pointing to a
    non-existent MinIO object), then trigger backup and verify the validation
    step detects the mismatch.
    """
    pg_pod = get_pg_pod()
    if not pg_pod:
        return 0.0, "No PostgreSQL pod found"

    # Insert orphan record
    ts = int(time.time())
    orphan_key = f"attachments/orphan_test/missing_object_{ts}.dmp"
    pg_sql(pg_pod,
        f"INSERT INTO files_fileblob (created, checksum, size, blob) "
        f"VALUES (NOW(), 'orphan_test_{ts}', 1024, '{orphan_key}') "
        f"ON CONFLICT (checksum) DO NOTHING;",
        setup_info,
    )

    # Trigger backup Job
    job_name = f"grader-orphan-{ts}"
    completed, logs, failed = trigger_backup_job(job_name)

    logs_lower = logs.lower()
    detected = any(w in logs_lower for w in [
        "validation fail", "mismatch", "inconsisten", "orphan",
        "missing", "not found in minio", "not found in backup",
        "pg has", "more records than", "fewer objects",
    ])

    job_failed = failed.isdigit() and int(failed) > 0

    if detected or job_failed:
        return 1.0, "Validation detected the orphan record mismatch"
    else:
        return 0.0, f"Validation did NOT catch the orphan. Logs: {logs[:300]}"


# ─────────────────────────────────────────────
# CHECK 4: backup_notifies_on_failure
# ─────────────────────────────────────────────
def check_backup_notifies_on_failure(setup_info):
    """
    FUNCTIONAL E2E: Deliberately break MinIO (scale to 0), trigger a backup,
    then check if the backup script wrote failure status to a discoverable
    location: ConfigMap, Kubernetes Event, file, or annotation.

    Agents never build failure notification. They add 'exit 1' but don't
    report the failure anywhere persistent. The grader checks for:
    - A ConfigMap with backup status/failure info
    - A Kubernetes Event created by the backup Job
    - An annotation on the CronJob with failure info
    - A log line with structured failure output (JSON or key=value)

    After checking, restore MinIO to original state.
    """
    # Save current MinIO replica count
    rc, replicas, _ = run_cmd(
        "kubectl get deployment glitchtip-minio -n glitchtip "
        "-o jsonpath='{.spec.replicas}' 2>/dev/null"
    )
    original_replicas = replicas.strip("'") or "1"

    # Break MinIO by scaling to 0
    run_cmd("kubectl scale deployment glitchtip-minio -n glitchtip --replicas=0 2>/dev/null")
    time.sleep(10)

    # Trigger a backup Job — it should fail because MinIO is down
    ts = int(time.time())
    job_name = f"grader-notify-{ts}"
    completed, logs, failed = trigger_backup_job(job_name, wait_seconds=180)

    # Restore MinIO immediately
    run_cmd(f"kubectl scale deployment glitchtip-minio -n glitchtip --replicas={original_replicas} 2>/dev/null")
    # Wait for MinIO to come back
    for i in range(30):
        rc, ready, _ = run_cmd(
            "kubectl get deployment glitchtip-minio -n glitchtip "
            "-o jsonpath='{.status.readyReplicas}' 2>/dev/null"
        )
        if ready.strip("'") == original_replicas:
            break
        time.sleep(5)

    # Now check if the agent's script NOTIFIED about the failure
    # Check 1: ConfigMap with backup status
    rc, cms, _ = run_cmd(
        "kubectl get configmaps -n glitchtip -o json 2>/dev/null"
    )
    has_status_cm = False
    if cms:
        try:
            cm_list = json.loads(cms)
            for cm in cm_list.get("items", []):
                cm_data = " ".join(cm.get("data", {}).values()).lower()
                cm_name = cm.get("metadata", {}).get("name", "").lower()
                if ("backup" in cm_name and ("fail" in cm_data or "error" in cm_data or "status" in cm_data)):
                    has_status_cm = True
                    break
                if "backup-status" in cm_name or "backup-report" in cm_name:
                    has_status_cm = True
                    break
        except json.JSONDecodeError:
            pass

    # Check 2: Annotation on the CronJob
    rc, annotations, _ = run_cmd(
        "kubectl get cronjob glitchtip-backup -n glitchtip "
        "-o jsonpath='{.metadata.annotations}' 2>/dev/null"
    )
    has_annotation = False
    if annotations:
        ann_lower = annotations.lower()
        has_annotation = any(w in ann_lower for w in ["fail", "error", "status", "last-result"])

    # Check 3: Kubernetes Events from backup
    rc, events, _ = run_cmd(
        f"kubectl get events -n glitchtip --field-selector reason!=Pulling,reason!=Pulled "
        f"--sort-by=.lastTimestamp -o json 2>/dev/null"
    )
    has_event = False
    if events:
        try:
            ev_list = json.loads(events)
            for ev in ev_list.get("items", []):
                msg = ev.get("message", "").lower()
                reason = ev.get("reason", "").lower()
                if "backup" in msg and ("fail" in msg or "error" in msg):
                    has_event = True
                    break
                if "backupfail" in reason or "backup-fail" in reason:
                    has_event = True
                    break
        except json.JSONDecodeError:
            pass

    # Check 4: Structured failure in backup logs
    has_structured_log = False
    if logs:
        logs_lower = logs.lower()
        # Look for structured output: JSON, key=value, or explicit notification
        has_structured_log = any(w in logs_lower for w in [
            '"status": "fail', '"status":"fail',
            "status=fail", "result=fail", "backup_status=fail",
            "notification", "alert", "webhook",
            "mattermost", "slack",
            "writing failure status", "updating backup status",
        ])

    notified = has_status_cm or has_annotation or has_event or has_structured_log

    details = (
        f"ConfigMap status={has_status_cm}, "
        f"CronJob annotation={has_annotation}, "
        f"K8s Event={has_event}, "
        f"Structured log={has_structured_log}"
    )

    if notified:
        return 1.0, f"Backup failure was reported: {details}"
    else:
        return 0.0, f"Backup failed but no notification found. {details}. Logs: {logs[:200]}"


# ─────────────────────────────────────────────
# GRADE
# ─────────────────────────────────────────────
def grade(*args, **kwargs) -> GradingResult:
    """Main grading function."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 180s durability window...")
    time.sleep(180)

    checks = {
        "backup_script_has_minio": check_backup_script_has_minio,
        "backup_handles_concurrent_writes": check_backup_handles_concurrent_writes,
        "validation_catches_orphans": check_validation_catches_orphans,
        "backup_notifies_on_failure": check_backup_notifies_on_failure,
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
        feedback_parts.append(
            f"[{name}] {'PASS' if score > 0 else 'FAIL'}: {feedback}"
        )
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
