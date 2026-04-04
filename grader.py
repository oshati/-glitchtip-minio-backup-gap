#!/usr/bin/env python3
"""
Grader for glitchtip-minio-backup-gap task.

ALL checks are STRICT FUNCTIONAL end-to-end tests that mutate the environment
AFTER the agent finishes, then test the agent's code against the new state.

4 subscores, each weight 1/4:
1. backup_script_has_minio — Backup ConfigMap has MinIO backup commands
2. validation_catches_orphans — PG record with no MinIO object: does validation catch it?
3. backup_detects_stale_objects — MinIO objects with no PG record: does validation catch the reverse?
4. backup_excludes_deleted_records — PG records deleted after agent finishes: does backup reflect current state?
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


def minio_exec(minio_pod, mc_cmd, setup_info):
    ak = setup_info.get("MINIO_ACCESS_KEY", "")
    sk = setup_info.get("MINIO_SECRET_KEY", "")
    rc, out, _ = run_cmd(
        f"kubectl exec -n glitchtip {minio_pod} -- "
        f"sh -c \"mc alias set local http://localhost:9000 '{ak}' '{sk}' "
        f">/dev/null 2>&1; {mc_cmd}\"",
        timeout=15,
    )
    return rc, out


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
# CHECK 2: validation_catches_orphans
# Agent's validation must detect PG records that
# have NO corresponding MinIO object (forward orphan).
# Grader inserts the orphan AFTER agent finishes.
# ─────────────────────────────────────────────
def check_validation_catches_orphans(setup_info):
    """
    Insert an orphan PG record (metadata pointing to a non-existent MinIO object),
    then trigger backup and verify the validation step detects the mismatch.
    """
    pg_pod = get_pg_pod()
    if not pg_pod:
        return 0.0, "No PostgreSQL pod found"

    ts = int(time.time())
    orphan_key = f"attachments/orphan_test/missing_object_{ts}.dmp"
    pg_sql(pg_pod,
        f"INSERT INTO files_fileblob (created, checksum, size, blob) "
        f"VALUES (NOW(), 'orphan_fwd_{ts}', 1024, '{orphan_key}') "
        f"ON CONFLICT (checksum) DO NOTHING;",
        setup_info,
    )

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
        return 1.0, "Validation detected the forward orphan (PG record with no MinIO object)"
    else:
        return 0.0, f"Validation did NOT catch the forward orphan. Logs: {logs[:300]}"


# ─────────────────────────────────────────────
# CHECK 3: backup_detects_stale_objects
# Agent's validation must detect MinIO objects that
# have NO corresponding PG record (reverse orphan).
# Grader adds MinIO objects with no PG record AFTER
# agent finishes.
# ─────────────────────────────────────────────
def check_backup_detects_stale_objects(setup_info):
    """
    Add 5 MinIO objects that have NO PG fileblob records (stale/orphaned objects).
    Then trigger backup and verify the validation detects the reverse mismatch:
    MinIO has more objects than PG has records.

    Agents build "PG count > MinIO count = fail" but almost never check
    "MinIO count > PG count = stale objects."
    """
    minio_pod = get_minio_pod()
    if not minio_pod:
        return 0.0, "No MinIO pod found"

    bucket = setup_info.get("MINIO_BUCKET", "glitchtip-attachments")
    ts = int(time.time())

    # Inject 5 stale MinIO objects with NO corresponding PG records
    for i in range(5):
        minio_exec(minio_pod,
            f"echo 'stale-orphan-data-{ts}-{i}' | mc pipe local/{bucket}/stale_objects/orphan_{ts}_{i}.dat 2>/dev/null",
            setup_info,
        )

    # Verify they were created
    rc, count_str = minio_exec(minio_pod,
        f"mc ls --recursive local/{bucket}/stale_objects/ 2>/dev/null | wc -l",
        setup_info,
    )
    stale_count = int(count_str.strip()) if count_str.strip().isdigit() else 0

    if stale_count < 3:
        return 0.0, f"Could not inject stale objects into MinIO (only {stale_count} created)"

    # Trigger backup Job
    job_name = f"grader-stale-{ts}"
    completed, logs, failed = trigger_backup_job(job_name)

    logs_lower = logs.lower()

    # Look for evidence the validation caught the REVERSE mismatch
    # (MinIO has more objects than PG records)
    detected = any(w in logs_lower for w in [
        "stale", "orphan", "extra object", "minio has more",
        "minio count", "mismatch", "inconsisten",
        "more objects than", "objects without record",
        "not in database", "not in pg", "no matching record",
    ])

    # Also accept: validation reports MinIO count > PG count as a warning/fail
    # Look for comparative output where MinIO > PG
    pg_pod = get_pg_pod()
    if pg_pod:
        pg_count_str = pg_sql(pg_pod,
            "SELECT COUNT(*) FROM files_fileblob;",
            setup_info,
        )
        pg_count = int(pg_count_str) if pg_count_str.isdigit() else 0

        # Check if logs mention both counts and the MinIO count is higher
        minio_mentions = re.findall(r'minio[^0-9]*(\d+)', logs_lower)
        pg_mentions = re.findall(r'pg[^0-9]*(\d+)', logs_lower)

        if minio_mentions and pg_mentions:
            max_minio = max(int(x) for x in minio_mentions)
            max_pg = max(int(x) for x in pg_mentions)
            if max_minio > max_pg:
                detected = True

    job_failed = failed.isdigit() and int(failed) > 0

    if detected or job_failed:
        return 1.0, f"Validation detected stale MinIO objects (reverse orphan). Stale injected: {stale_count}"
    else:
        return 0.0, f"Validation did NOT detect {stale_count} stale MinIO objects. Logs: {logs[:300]}"


# ─────────────────────────────────────────────
# CHECK 4: backup_excludes_deleted_records
# Grader deletes PG records AFTER agent finishes,
# then triggers backup. The backup's PG dump should
# reflect the current (reduced) state. The validation
# should report fewer PG records than MinIO objects.
# ─────────────────────────────────────────────
def check_backup_excludes_deleted_records(setup_info):
    """
    Delete 5 PG fileblob records AFTER the agent finishes (simulating data
    cleanup between backup runs). Then trigger backup and verify:
    1. The backup's PG dump reflects the CURRENT state (fewer records)
    2. The validation detects that MinIO now has MORE objects than PG records

    Agents assume PG and MinIO are always in sync at backup time. After deletion,
    MinIO has objects that PG no longer references — this is the reverse of the
    orphan check. The agent's validation must be bidirectional.
    """
    pg_pod = get_pg_pod()
    if not pg_pod:
        return 0.0, "No PostgreSQL pod found"

    # Get current PG count before deletion
    before_str = pg_sql(pg_pod,
        "SELECT COUNT(*) FROM files_fileblob WHERE blob NOT LIKE 'attachments/orphan_test%' AND blob NOT LIKE 'concurrent/%' AND blob NOT LIKE 'stale_objects/%';",
        setup_info,
    )
    before_count = int(before_str) if before_str.isdigit() else 0

    if before_count < 10:
        return 0.0, f"Not enough PG records to delete (only {before_count})"

    # Delete 5 debug-symbol records (they have MinIO objects but we remove PG refs)
    ts = int(time.time())
    pg_sql(pg_pod,
        "DELETE FROM files_fileblob WHERE blob LIKE 'debug-symbols/%' LIMIT 5;",
        setup_info,
    )

    # Verify deletion worked
    after_str = pg_sql(pg_pod,
        "SELECT COUNT(*) FROM files_fileblob WHERE blob NOT LIKE 'attachments/orphan_test%' AND blob NOT LIKE 'concurrent/%' AND blob NOT LIKE 'stale_objects/%';",
        setup_info,
    )
    after_count = int(after_str) if after_str.isdigit() else 0
    deleted = before_count - after_count

    if deleted < 1:
        # Try alternative deletion
        pg_sql(pg_pod,
            f"DELETE FROM files_fileblob WHERE checksum LIKE 'sha1_dsym_%';",
            setup_info,
        )
        after_str = pg_sql(pg_pod,
            "SELECT COUNT(*) FROM files_fileblob WHERE blob NOT LIKE 'attachments/orphan_test%' AND blob NOT LIKE 'concurrent/%' AND blob NOT LIKE 'stale_objects/%';",
            setup_info,
        )
        after_count = int(after_str) if after_str.isdigit() else 0
        deleted = before_count - after_count

    if deleted < 1:
        return 0.0, f"Could not delete PG records (before={before_count}, after={after_count})"

    # Trigger backup Job
    job_name = f"grader-deleted-{ts}"
    completed, logs, failed = trigger_backup_job(job_name)

    logs_lower = logs.lower()

    # The validation should detect MinIO has more objects than PG records
    detected = any(w in logs_lower for w in [
        "stale", "orphan", "mismatch", "inconsisten",
        "minio has more", "more objects than records",
        "extra object", "not in database", "not in pg",
        "objects without record", "no matching record",
    ])

    # Check if the validation reports counts where MinIO > PG
    minio_mentions = re.findall(r'minio[^0-9]*(\d+)', logs_lower)
    pg_mentions = re.findall(r'pg[^0-9]*(\d+)', logs_lower)
    if minio_mentions and pg_mentions:
        max_minio = max(int(x) for x in minio_mentions)
        max_pg = max(int(x) for x in pg_mentions)
        if max_minio > max_pg:
            detected = True

    job_failed = failed.isdigit() and int(failed) > 0

    if detected or job_failed:
        return 1.0, f"Validation detected deleted PG records ({deleted} removed, MinIO still has objects)"
    else:
        return 0.0, f"Validation did NOT detect {deleted} deleted PG records. Logs: {logs[:300]}"


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
        "validation_catches_orphans": check_validation_catches_orphans,
        "backup_detects_stale_objects": check_backup_detects_stale_objects,
        "backup_excludes_deleted_records": check_backup_excludes_deleted_records,
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
