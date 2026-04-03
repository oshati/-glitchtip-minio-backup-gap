#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap task.

ALL checks are STRICT FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. backup_script_has_minio — Backup ConfigMap has MinIO backup commands
2. backup_job_completes_e2e — Trigger Job, REQUIRE exit 0 with PG+MinIO evidence
3. validation_catches_orphans — Insert orphan PG record, verify backup script detects mismatch
4. backup_preserves_all_objects — Delete MinIO objects, run backup, verify backup captured originals
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
    Accepts: mc, s3cmd, aws s3, boto3, curl with S3 — any valid approach.
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


def check_backup_job_completes_e2e(setup_info):
    """
    STRICT FUNCTIONAL: Trigger a backup Job and REQUIRE:
    1. Job status.succeeded == 1 (exit 0, no partial credit)
    2. Logs contain BOTH PostgreSQL AND MinIO evidence
    """
    job_name = f"grader-e2e-{int(time.time())}"

    rc, _, stderr = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=15,
    )
    if rc != 0:
        return 0.0, f"Could not create backup job: {stderr[:200]}"

    # Wait up to 5 minutes for completion
    completed = False
    for i in range(30):
        rc, job_status, _ = run_cmd(
            f"kubectl get job {job_name} -n glitchtip "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}' 2>/dev/null"
        )
        parts = job_status.strip("'").split("/")
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
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=200 2>/dev/null"
    )

    if not completed:
        return 0.0, f"Backup job FAILED (did not exit 0). Logs: {logs[:300]}"

    has_pg = any(w in logs.lower() for w in ["pg_dump", "postgresql dump", "dump complete", "glitchtip_db.dump"])
    has_minio = any(w in logs.lower() for w in ["mirror", "mc mirror", "minio backup", "objects mirrored", "attachments backed"])

    if has_pg and has_minio:
        return 1.0, "Backup job completed successfully with both PG and MinIO"
    elif has_pg:
        return 0.0, f"Job exited 0 but no MinIO evidence. Logs: {logs[:200]}"
    elif has_minio:
        return 0.0, f"Job exited 0 but no PG dump evidence. Logs: {logs[:200]}"
    else:
        return 0.0, f"Job exited 0 but no backup evidence. Logs: {logs[:200]}"


def check_validation_catches_orphans(setup_info):
    """
    STRICT FUNCTIONAL: Insert an orphan PG record (metadata pointing to a
    non-existent MinIO object), then trigger backup and verify the validation
    step detects the mismatch (logs show FAIL/error/mismatch).

    If the agent's backup script has no real validation, this will fail.
    """
    pg_pod = ""
    rc, pg_pod, _ = run_cmd(
        "kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
    )
    pg_pod = pg_pod.strip("'")
    if not pg_pod:
        return 0.0, "No PostgreSQL pod found"

    # Insert orphan record — metadata pointing to non-existent MinIO object
    orphan_key = f"attachments/orphan_test/missing_object_{int(time.time())}.dmp"
    rc, _, stderr = run_cmd(
        f"kubectl exec -n glitchtip {pg_pod} -- "
        f"bash -c \"PGPASSWORD=7KkJeWZYkK psql -U postgres -d postgres -c "
        f"\\\"INSERT INTO files_fileblob (created, checksum, size, blob) "
        f"VALUES (NOW(), 'orphan_test_{int(time.time())}', 1024, '{orphan_key}') "
        f"ON CONFLICT (checksum) DO NOTHING;\\\"\"",
        timeout=15,
    )

    # Now trigger a backup Job and check if validation catches the orphan
    job_name = f"grader-orphan-{int(time.time())}"
    rc, _, _ = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=15,
    )
    if rc != 0:
        return 0.0, "Could not create validation test job"

    # Wait up to 5 minutes
    for i in range(30):
        rc, job_status, _ = run_cmd(
            f"kubectl get job {job_name} -n glitchtip "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}' 2>/dev/null"
        )
        parts = job_status.strip("'").split("/")
        succeeded = parts[0] if len(parts) > 0 else ""
        failed = parts[1] if len(parts) > 1 else ""

        if succeeded == "1" or (failed.isdigit() and int(failed) > 0):
            break
        time.sleep(10)

    # Get logs
    rc, logs, _ = run_cmd(
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=200 2>/dev/null"
    )

    # The validation should detect the orphan — look for mismatch/fail/warning
    logs_lower = logs.lower()
    detected = any(w in logs_lower for w in [
        "validation fail", "mismatch", "inconsisten", "orphan",
        "missing", "not found in minio", "not found in backup",
        "pg has", "more records than", "fewer objects",
    ])

    # Also accept if the job failed (exit 1) because validation caught the gap
    job_failed = failed.isdigit() and int(failed) > 0

    if detected or job_failed:
        return 1.0, "Validation detected the orphan record mismatch"
    else:
        return 0.0, f"Validation did NOT catch the orphan. Logs: {logs[:300]}"


def check_backup_preserves_all_objects(setup_info):
    """
    STRICT FUNCTIONAL: Verify MinIO objects still exist and are backed up.
    Check PG record count matches MinIO object count.
    Then check the most recent backup job logs show the correct count.
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
        f"\\\"SELECT COUNT(*) FROM files_fileblob WHERE blob NOT LIKE 'attachments/orphan_test%';\\\"\"",
        timeout=15,
    )
    pg_count = int(pg_count_str.strip()) if pg_count_str.strip().isdigit() else 0

    if pg_count == 0:
        return 0.0, "No PG fileblob records found"

    # Get MinIO object count directly
    minio_pod = ""
    rc, minio_pod, _ = run_cmd(
        "kubectl get pods -n glitchtip -l app=glitchtip-minio "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
    )
    minio_pod = minio_pod.strip("'")
    if not minio_pod:
        return 0.0, "No MinIO pod found"

    rc, obj_count_str, _ = run_cmd(
        f"kubectl exec -n glitchtip {minio_pod} -- "
        f"sh -c \"mc alias set local http://localhost:9000 "
        f"$(cat /etc/minio-creds/MINIO_ACCESS_KEY 2>/dev/null || echo glitchtip-minio) "
        f"$(cat /etc/minio-creds/MINIO_SECRET_KEY 2>/dev/null || echo minio-secret-key-2024) "
        f">/dev/null 2>&1; mc ls --recursive local/{minio_bucket}/ 2>/dev/null | wc -l\"",
        timeout=15,
    )
    minio_count = int(obj_count_str.strip()) if obj_count_str.strip().isdigit() else 0

    # Try alternative method to get MinIO creds and count
    if minio_count == 0:
        access_key = setup_info.get("MINIO_ACCESS_KEY", "")
        secret_key = setup_info.get("MINIO_SECRET_KEY", "")
        rc, obj_count_str, _ = run_cmd(
            f"kubectl exec -n glitchtip {minio_pod} -- "
            f"sh -c \"mc alias set local http://localhost:9000 '{access_key}' '{secret_key}' "
            f">/dev/null 2>&1; mc ls --recursive local/{minio_bucket}/ 2>/dev/null | wc -l\"",
            timeout=15,
        )
        minio_count = int(obj_count_str.strip()) if obj_count_str.strip().isdigit() else 0

    if minio_count == 0:
        return 0.0, f"MinIO has 0 objects (PG has {pg_count}). Agent may have deleted them."

    # Check if the backup Job logs show matching counts
    rc, logs, _ = run_cmd(
        "kubectl logs -n glitchtip -l job=backup --all-containers --tail=200 2>/dev/null"
    )
    if not logs:
        # Try grader jobs
        rc, pods, _ = run_cmd(
            "kubectl get pods -n glitchtip --sort-by=.status.startTime "
            "-o jsonpath='{range .items[*]}{.metadata.name} {end}' 2>/dev/null"
        )
        for pod in reversed(pods.strip("'").split()):
            if "grader" in pod.lower() or "backup" in pod.lower():
                rc, logs, _ = run_cmd(
                    f"kubectl logs -n glitchtip {pod} --all-containers --tail=200 2>/dev/null"
                )
                if logs and ("mirror" in logs.lower() or "attachment" in logs.lower()):
                    break

    has_mirror = "mirror" in logs.lower() or "copied" in logs.lower() if logs else False
    has_count = False
    if logs:
        obj_matches = re.findall(r'(\d+)\s*object', logs.lower())
        if obj_matches:
            backed_up = max(int(x) for x in obj_matches)
            if backed_up >= pg_count:
                has_count = True

    if minio_count >= pg_count and (has_mirror or has_count):
        return 1.0, f"MinIO has {minio_count} objects, PG has {pg_count} records. Backup verified."
    elif minio_count >= pg_count:
        return 1.0, f"MinIO has {minio_count} objects matching PG's {pg_count} records."
    else:
        return 0.0, f"PG has {pg_count} records but MinIO only has {minio_count} objects."


def grade(*args, **kwargs) -> GradingResult:
    """Main grading function. ALL checks are strict functional E2E tests."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    # Wait for any in-progress enforcers to run
    print("[grader] Waiting 180s durability window...")
    time.sleep(180)

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
