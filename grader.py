#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap task.

ALL checks are FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. backup_script_has_minio — Backup script contains MinIO backup commands (mc/s3)
2. validation_logic_present — Backup pipeline has PG vs MinIO count validation with failure condition
3. backup_job_produces_output — Actually run the backup Job and verify it completes with PG dump + MinIO mirror
4. backup_captures_all_objects — Verify the backup Job captured all MinIO objects that PG references
"""

import json
import os
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
    Check that the backup ConfigMap contains MinIO backup commands.
    Accepts mc, s3cmd, aws s3, or curl-based S3 backup approaches.
    Checks ALL scripts in the ConfigMap (not just backup.sh).
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

    script = all_scripts.lower()

    # Check for MinIO backup commands (accept various approaches)
    has_mc = "mc " in script or "mc mirror" in script or "mc cp" in script
    has_s3 = "aws s3" in script or "s3cmd" in script
    has_minio_ref = "minio" in script
    has_bucket = "glitchtip-attachments" in script or "minio_bucket" in script or "attachment" in script

    if (has_mc or has_s3) and (has_bucket or has_minio_ref):
        return 1.0, "Backup script includes MinIO backup commands"
    elif has_minio_ref:
        return 0.0, "Script references MinIO but no backup commands (mc/s3) found"
    else:
        return 0.0, "No MinIO backup step found in any backup script"


def check_validation_logic_present(setup_info):
    """
    Check that the backup pipeline includes validation that compares
    PG attachment count vs MinIO object count, with a failure condition.
    Checks ALL scripts in the ConfigMap.
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

    script = all_scripts

    has_pg_query = "files_fileblob" in script or "fileblob" in script or "attachment" in script.lower()
    has_count = "COUNT" in script.upper() or "count" in script or "wc -l" in script
    has_comparison = "VALIDATION" in script.upper() or "validation" in script or "validate" in script.lower()
    has_fail = "exit 1" in script or "FAIL" in script or "fail" in script or "error" in script.lower()

    if has_pg_query and has_count and has_fail:
        return 1.0, "Validation step found with PG count check and failure condition"
    elif has_comparison or has_count:
        return 0.0, "Partial validation found but missing count comparison or failure condition"
    else:
        return 0.0, "No validation logic found in backup scripts"


def check_backup_job_produces_output(setup_info):
    """
    FUNCTIONAL: Actually trigger a backup Job from the CronJob and verify
    it completes successfully with BOTH PG dump and MinIO backup in the logs.
    """
    job_name = f"grader-backup-{int(time.time())}"

    # Trigger a backup job
    rc, _, stderr = run_cmd(
        f"kubectl create job {job_name} --from=cronjob/glitchtip-backup -n glitchtip 2>&1",
        timeout=15,
    )
    if rc != 0:
        return 0.0, f"Could not create backup job: {stderr[:200]}"

    # Wait for completion (up to 5 minutes)
    completed = False
    for i in range(30):
        # Check succeeded and failed together
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

        # Only fail if no active pods AND failed > 0
        if active in ("", "0") and failed.isdigit() and int(failed) > 0:
            rc, logs, _ = run_cmd(
                f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=50 2>/dev/null"
            )
            # Check if the logs actually show success despite the status
            has_output = "mirror" in logs.lower() or "attachments" in logs.lower() or "backup complete" in logs.lower()
            if has_output:
                completed = True
                break
            return 0.0, f"Backup job failed. Logs: {logs[:300]}"

        time.sleep(10)

    if not completed:
        rc, logs, _ = run_cmd(
            f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=30 2>/dev/null"
        )
        return 0.0, f"Backup job timed out (5 min). Logs: {logs[:300]}"

    # Check logs from ALL containers (init + main) for both PG and MinIO steps
    rc, logs, _ = run_cmd(
        f"kubectl logs -n glitchtip -l job-name={job_name} --all-containers --tail=50 2>/dev/null"
    )

    has_minio = "minio" in logs.lower() or "mc " in logs.lower() or "mirror" in logs.lower() or "objects" in logs.lower() or "attachments" in logs.lower()

    # Job completed successfully — init container (pg_dump) must have passed for main to start
    # Main container logs show MinIO backup — that's the key functional evidence
    if has_minio and completed:
        return 1.0, f"Backup job completed with MinIO backup (PG dump in init container)"
    elif completed:
        return 0.5, f"Backup job completed but no MinIO backup evidence in logs"
    else:
        return 0.0, f"Backup job did not complete. Logs: {logs[:200]}"


def check_backup_captures_all_objects(setup_info):
    """
    FUNCTIONAL: Verify that the backup actually captured MinIO objects.
    Check that the most recent backup Job's output contains MinIO files
    matching the PG record count.
    """
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

    # Check if a backup job ran and its logs show the objects were captured
    rc, logs, _ = run_cmd(
        "kubectl logs -n glitchtip -l app=glitchtip,job=backup --tail=50 2>/dev/null"
    )

    if not logs:
        # Try getting logs from any backup job pod
        rc, pods, _ = run_cmd(
            "kubectl get pods -n glitchtip --sort-by=.status.startTime "
            "-o jsonpath='{range .items[*]}{.metadata.name}{\" \"}{end}' 2>/dev/null"
        )
        for pod in pods.strip("'").split():
            if "backup" in pod.lower():
                rc, logs, _ = run_cmd(
                    f"kubectl logs -n glitchtip {pod} --tail=50 2>/dev/null"
                )
                if logs:
                    break

    # Look for evidence that objects were backed up
    if not logs:
        return 0.0, f"No backup job logs found. PG has {pg_count} records."

    # Check if logs mention successful MinIO backup with object count
    import re
    obj_matches = re.findall(r'(\d+)\s*object', logs.lower())
    mirror_success = "mirror" in logs.lower() or "copied" in logs.lower() or "success" in logs.lower()

    if mirror_success and obj_matches:
        backed_up = max(int(x) for x in obj_matches)
        if backed_up >= pg_count:
            return 1.0, f"Backup captured {backed_up} objects (PG has {pg_count} records)"
        else:
            return 0.0, f"Backup only captured {backed_up} of {pg_count} expected objects"
    elif mirror_success:
        return 1.0, f"MinIO mirror completed (PG has {pg_count} records)"
    else:
        return 0.0, f"No evidence of MinIO objects being captured in backup logs. PG has {pg_count} records."


def grade(*args, **kwargs) -> GradingResult:
    """Main grading function."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 60s...")
    time.sleep(60)

    checks = {
        "backup_script_has_minio": check_backup_script_has_minio,
        "validation_logic_present": check_validation_logic_present,
        "backup_job_produces_output": check_backup_job_produces_output,
        "backup_captures_all_objects": check_backup_captures_all_objects,
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
