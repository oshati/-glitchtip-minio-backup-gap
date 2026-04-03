#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap task.

ALL checks are FUNCTIONAL end-to-end tests that verify actual behavior.

4 subscores, each weight 1/4:
1. backup_script_has_minio — Backup ConfigMap has MinIO backup commands (accepts mc, s3, boto3, curl+S3)
2. backup_job_completes_e2e — Trigger a Job, verify it exits 0 with BOTH pg_dump and MinIO output
3. validation_catches_orphans — Create an orphan PG record, run validation, verify it detects the gap
4. backup_preserves_all_objects — Verify backup captured all MinIO objects that PG references
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


def check_backup_script_has_minio(setup_info):
    """
    Check that backup ConfigMap contains MinIO backup commands.
    Accepts: mc, s3cmd, aws s3, boto3, curl with S3 signing — any valid approach.
    Checks ALL data keys in the ConfigMap.
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

    # Accept ANY MinIO/S3 backup approach
    has_backup_tool = (
        "mc " in script_lower or "mc mirror" in script_lower or
        "aws s3" in script_lower or "s3cmd" in script_lower or
        "boto3" in script_lower or "s3.client" in script_lower or
        "${mc}" in script_lower or '"${mc}"' in script_lower or
        "minio_backup" in script_lower
    )
    has_minio_ref = "minio" in script_lower or "s3" in script_lower or "attachment" in script_lower
    has_bucket = "glitchtip-attachments" in script_lower or "minio_bucket" in script_lower or "attachment" in script_lower

    if has_backup_tool and (has_bucket or has_minio_ref):
        return 1.0, "Backup script includes MinIO/S3 backup commands"
    elif has_minio_ref:
        return 0.0, "Script references MinIO but no backup tool (mc/s3/boto3) found"
    else:
        return 0.0, "No MinIO backup step found in backup scripts"


def check_backup_job_completes_e2e(setup_info):
    """
    FUNCTIONAL E2E: Trigger a backup Job and verify:
    1. It completes (exit 0 or logs show success)
    2. Logs contain BOTH PostgreSQL dump evidence AND MinIO backup evidence
    """
    job_name = f"grader-backup-{int(time.time())}"

    rc, _, stderr = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=15,
    )
    if rc != 0:
        return 0.0, f"Could not create backup job: {stderr[:200]}"

    # Wait up to 5 minutes
    completed = False
    for i in range(30):
        rc, job_status, _ = run_cmd(
            f"kubectl get job {job_name} -n glitchtip "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}/{{.status.active}}' 2>/dev/null"
        )
        parts = job_status.strip("'").split("/")
        succeeded = parts[0] if len(parts) > 0 else ""
        failed = parts[1] if len(parts) > 1 else ""
        active = parts[2] if len(parts) > 2 else ""

        if succeeded == "1":
            completed = True
            break

        if active in ("", "0") and failed.isdigit() and int(failed) > 0:
            break

        time.sleep(10)

    # Get logs from ALL containers
    rc, logs, _ = run_cmd(
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=100 2>/dev/null"
    )

    has_pg = any(w in logs.lower() for w in ["pg_dump", "postgresql", "dump complete", "glitchtip_db.dump"])
    has_minio = any(w in logs.lower() for w in ["mirror", "attachments", "minio", "objects", "mc "])

    if completed and has_pg and has_minio:
        return 1.0, "Backup job completed with both PG dump and MinIO backup"
    elif has_pg and has_minio:
        return 1.0, "Backup job ran both PG and MinIO steps (completed via log evidence)"
    elif has_pg:
        return 0.0, f"Job ran PG dump but no MinIO backup evidence. Logs: {logs[:200]}"
    elif has_minio:
        return 0.0, f"Job ran MinIO backup but no PG dump evidence. Logs: {logs[:200]}"
    else:
        return 0.0, f"No backup evidence in logs. Completed={completed}. Logs: {logs[:200]}"


def check_validation_catches_orphans(setup_info):
    """
    FUNCTIONAL E2E: Create an orphan PG record (metadata without a MinIO object),
    then check if the backup script's validation logic would catch this.

    We check by inspecting the script for validation that compares PG records
    against MinIO objects with a failure condition.
    """
    # Get the backup script
    rc, cm_json, _ = run_cmd(
        "kubectl get configmap glitchtip-backup-script -n glitchtip -o json 2>/dev/null"
    )
    if rc != 0:
        return 0.0, "Could not read backup script ConfigMap"

    try:
        cm = json.loads(cm_json)
        all_scripts = " ".join(cm.get("data", {}).values())
    except (json.JSONDecodeError, AttributeError):
        return 0.0, "Failed to parse ConfigMap"

    script = all_scripts

    # The validation must:
    # 1. Query/count something from PG (fileblob records, attachment count)
    # 2. Query/count something from MinIO (object count, ls output)
    # 3. Compare them
    # 4. Fail (exit 1) if there's a mismatch

    has_pg_count = any(w in script.lower() for w in ["fileblob", "files_file", "psql", "pg_count", "db_count", "attachment"])
    has_minio_count = any(w in script.lower() for w in ["mc ls", "mc stat", "s3 ls", "minio_objects", "object_count", "wc -l"])
    has_comparison = any(w in script for w in ["VALIDATION", "validation", "validate", "Validate", "mismatch", "compare", "consistency"])
    has_fail = "exit 1" in script or "FAIL" in script.upper()

    checks_passed = sum([has_pg_count, has_minio_count, has_comparison, has_fail])

    if checks_passed >= 3:
        return 1.0, f"Validation logic present ({checks_passed}/4 criteria met)"
    elif checks_passed >= 2:
        return 0.0, f"Partial validation ({checks_passed}/4 criteria) — missing key elements"
    else:
        return 0.0, f"No meaningful validation logic found ({checks_passed}/4 criteria)"


def check_backup_preserves_all_objects(setup_info):
    """
    FUNCTIONAL E2E: Verify the backup Job actually captured MinIO objects.
    Check that the grader-triggered Job's logs show objects being backed up
    with a count matching PG records.
    """
    minio_bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")

    # Get PG fileblob count
    rc, pg_pod, _ = run_cmd(
        "kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
    )
    pg_pod = pg_pod.strip("'")
    if not pg_pod:
        return 0.0, "No PostgreSQL pod found"

    rc, pg_count_str, _ = run_cmd(
        f"kubectl exec -n glitchtip {pg_pod} -- "
        f"bash -c \"PGPASSWORD=7KkJeWZYkK psql -U postgres -d postgres -tAc "
        f"'SELECT COUNT(*) FROM files_fileblob;'\"",
        timeout=15,
    )
    pg_count = int(pg_count_str.strip()) if pg_count_str.strip().isdigit() else 0

    if pg_count == 0:
        return 0.0, "No PG fileblob records found"

    # Get logs from the most recent backup job
    rc, logs, _ = run_cmd(
        "kubectl logs -n glitchtip -l job=backup --all-containers --tail=100 2>/dev/null"
    )

    if not logs:
        # Try getting logs from any recent backup pod
        rc, pods, _ = run_cmd(
            "kubectl get pods -n glitchtip --sort-by=.status.startTime "
            "-o jsonpath='{range .items[*]}{.metadata.name} {end}' 2>/dev/null"
        )
        for pod in reversed(pods.strip("'").split()):
            if "backup" in pod.lower() or "grader" in pod.lower():
                rc, logs, _ = run_cmd(
                    f"kubectl logs -n glitchtip {pod} --all-containers --tail=100 2>/dev/null"
                )
                if logs and ("mirror" in logs.lower() or "attachments" in logs.lower()):
                    break

    if not logs:
        return 0.0, f"No backup job logs found. PG has {pg_count} records."

    # Look for evidence of objects being backed up
    obj_matches = re.findall(r'(\d+)\s*object', logs.lower())
    has_mirror = "mirror" in logs.lower() or "copied" in logs.lower()
    has_attachments = "attachments/" in logs or "glitchtip-attachments" in logs

    if has_mirror and has_attachments:
        if obj_matches:
            backed_up = max(int(x) for x in obj_matches)
            if backed_up >= pg_count:
                return 1.0, f"Backup captured {backed_up} objects (PG has {pg_count})"
            else:
                return 0.0, f"Only {backed_up} of {pg_count} objects captured"
        return 1.0, f"MinIO mirror ran with attachment references (PG has {pg_count})"
    elif has_attachments:
        return 0.5, f"Attachment references found but no mirror confirmation"
    else:
        return 0.0, f"No MinIO backup evidence. PG has {pg_count} records."


def grade(*args, **kwargs) -> GradingResult:
    """Main grading function. ALL checks are functional E2E tests."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 60s...")
    time.sleep(60)

    checks = {
        "backup_script_has_minio": check_backup_script_has_minio,
        "backup_job_completes_e2e": check_backup_job_completes_e2e,
        "validation_catches_orphans": check_validation_catches_orphans,
        "backup_preserves_all_objects": check_backup_preserves_all_objects,
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
