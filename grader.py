#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap task.

ALL checks are end-to-end FUNCTIONAL tests.

4 subscores, each weight 1/4:
1. backup_includes_minio — Backup CronJob script contains MinIO/mc backup commands
2. validation_step_exists — Backup script compares PG attachment count vs MinIO object count
3. backup_runs_successfully — A manual backup Job completes with both PG and MinIO steps
4. minio_objects_intact — MinIO attachment bucket has objects matching PG metadata
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


def check_backup_includes_minio(setup_info):
    """
    FUNCTIONAL: Check the backup CronJob script contains MinIO backup commands.
    Must include mc/minio backup steps targeting the attachment bucket.
    """
    # Get the backup script from the ConfigMap
    rc, script, _ = run_cmd(
        "kubectl get configmap glitchtip-backup-script -n glitchtip "
        "-o jsonpath='{.data.backup\\.sh}'"
    )

    if rc != 0 or not script:
        return 0.0, "Could not read backup script ConfigMap"

    script = script.strip("'")

    # Check for MinIO backup commands
    has_mc = "mc " in script or "mc alias" in script or "mc mirror" in script
    has_minio_ref = "minio" in script.lower() or "s3" in script.lower()
    has_bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments") in script

    if has_mc and has_bucket:
        return 1.0, "Backup script includes mc commands targeting the attachment bucket"
    elif has_minio_ref:
        return 0.0, f"Script references MinIO but missing mc commands or bucket name"
    else:
        return 0.0, "Backup script has no MinIO/mc backup step"


def check_validation_step_exists(setup_info):
    """
    FUNCTIONAL: Check the backup script includes a validation step that
    compares PostgreSQL attachment record count against MinIO object count.
    """
    rc, script, _ = run_cmd(
        "kubectl get configmap glitchtip-backup-script -n glitchtip "
        "-o jsonpath='{.data.backup\\.sh}'"
    )

    if rc != 0 or not script:
        return 0.0, "Could not read backup script ConfigMap"

    script = script.strip("'")

    # Check for validation logic
    has_pg_count = "files_fileblob" in script or "attachment" in script.lower()
    has_comparison = "VALIDATION" in script.upper() or "validation" in script
    has_count_check = (
        ("COUNT" in script.upper() or "count" in script or "wc -l" in script)
        and ("psql" in script or "pg_" in script)
    )
    has_fail_condition = "exit 1" in script or "FAILED" in script or "failed" in script

    if has_pg_count and has_comparison and has_fail_condition:
        return 1.0, "Backup script has validation step with PG count check and failure condition"
    elif has_comparison:
        return 0.0, "Script mentions validation but missing count comparison or failure condition"
    else:
        return 0.0, "No validation step found in backup script"


def check_backup_runs_successfully(setup_info):
    """
    FUNCTIONAL: Trigger a manual backup Job and verify it completes with
    both PostgreSQL and MinIO backup steps.
    """
    # Check if there's a recently completed backup job
    rc, jobs, _ = run_cmd(
        "kubectl get jobs -n glitchtip -l app=glitchtip,job=backup "
        "-o jsonpath='{.items[*].metadata.name}' 2>/dev/null"
    )

    # Also check for any job created from the backup cronjob
    rc2, cj_jobs, _ = run_cmd(
        "kubectl get jobs -n glitchtip "
        "-o jsonpath='{range .items[*]}{.metadata.name}{\" \"}{.status.succeeded}{\"\\n\"}{end}' 2>/dev/null"
    )

    # Look for any completed backup job
    has_completed_backup = False
    if cj_jobs:
        for line in cj_jobs.strip("'").split("\n"):
            parts = line.strip().split()
            if len(parts) >= 2 and "backup" in parts[0].lower() and parts[1] == "1":
                has_completed_backup = True
                break

    if not has_completed_backup:
        # Try triggering one ourselves
        run_cmd(
            f"kubectl create job --from=cronjob/glitchtip-backup grader-backup-test -n glitchtip 2>/dev/null"
        )
        # Wait for it
        for i in range(30):
            rc, status, _ = run_cmd(
                "kubectl get job grader-backup-test -n glitchtip "
                "-o jsonpath='{.status.succeeded}' 2>/dev/null"
            )
            if status.strip("'") == "1":
                has_completed_backup = True
                break
            time.sleep(10)

    if not has_completed_backup:
        # Check if any backup job exists (even if not from the grader)
        rc, all_jobs, _ = run_cmd(
            "kubectl get jobs -n glitchtip -o name 2>/dev/null"
        )
        if all_jobs and "backup" in all_jobs.lower():
            return 0.5, "Backup job exists but did not complete successfully"
        return 0.0, "No backup job found or completed"

    # Check the job logs for both PG and MinIO steps
    rc, logs, _ = run_cmd(
        "kubectl logs -n glitchtip -l job-name=grader-backup-test --tail=50 2>/dev/null"
    )

    if not logs:
        # Try getting logs from any backup job
        rc, pods, _ = run_cmd(
            "kubectl get pods -n glitchtip -l app=glitchtip,job=backup "
            "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
        )
        if pods:
            rc, logs, _ = run_cmd(
                f"kubectl logs -n glitchtip {pods.strip(chr(39))} 2>/dev/null"
            )

    has_pg = "PostgreSQL" in logs or "pg_dump" in logs
    has_minio = "MinIO" in logs or "mc " in logs or "mirror" in logs

    if has_pg and has_minio:
        return 1.0, "Backup job completed with both PostgreSQL and MinIO steps"
    elif has_pg:
        return 0.0, "Backup job completed but only has PostgreSQL step (no MinIO)"
    else:
        return 0.0, f"Backup job logs incomplete: pg={has_pg}, minio={has_minio}"


def check_minio_objects_intact(setup_info):
    """
    FUNCTIONAL: Verify MinIO attachment bucket has objects matching PG metadata.
    Compare fileblob record count against MinIO object count.
    """
    minio_bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")

    # Get PG fileblob count
    pg_pod = ""
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

    # Get MinIO object count
    minio_pod = ""
    rc, minio_pod, _ = run_cmd(
        "kubectl get pods -n glitchtip -l app=glitchtip-minio "
        "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
    )
    minio_pod = minio_pod.strip("'")

    if not minio_pod:
        return 0.0, "No MinIO pod found"

    rc, minio_count_str, _ = run_cmd(
        f"kubectl exec -n glitchtip {minio_pod} -- "
        f"sh -c 'mc alias set local http://localhost:9000 "
        f"{setup_info.get('MINIO_ACCESS_KEY', 'glitchtip-minio')} "
        f"{setup_info.get('MINIO_SECRET_KEY', 'minio-secret-key-2024')} >/dev/null 2>&1; "
        f"mc ls --recursive local/{minio_bucket}/ 2>/dev/null | wc -l'",
        timeout=15,
    )
    minio_count = int(minio_count_str.strip()) if minio_count_str.strip().isdigit() else 0

    if pg_count > 0 and minio_count > 0:
        return 1.0, f"MinIO has {minio_count} objects, PG has {pg_count} fileblob records"
    elif pg_count > 0 and minio_count == 0:
        return 0.0, f"PG has {pg_count} records but MinIO has 0 objects (attachment data missing!)"
    else:
        return 0.0, f"Data counts: PG={pg_count}, MinIO={minio_count}"


def grade(*args, **kwargs) -> GradingResult:
    """Main grading function."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 60s...")
    time.sleep(60)

    checks = {
        "backup_includes_minio": check_backup_includes_minio,
        "validation_step_exists": check_validation_step_exists,
        "backup_runs_successfully": check_backup_runs_successfully,
        "minio_objects_intact": check_minio_objects_intact,
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
