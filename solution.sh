#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Inspecting the current backup pipeline..."
kubectl get cronjob glitchtip-backup -n glitchtip >/dev/null
kubectl get configmap glitchtip-backup-script -n glitchtip >/dev/null
kubectl get configmap glitchtip-backup-script-original -n glitchtip >/dev/null
kubectl get configmap glitchtip-backup-runtime-original -n glitchtip >/dev/null
kubectl get configmap glitchtip-backup-restore-handoff -n glitchtip >/dev/null
kubectl get cronjob glitchtip-backup-template-manager -n glitchtip >/dev/null

echo "[solution] Current incident: both the backup script and the backup runtime identity are reconciled through compliance baselines."
echo "[solution] Writing a replacement script that performs exact set and content-integrity validation and refreshes both the operator note and restore handoff."

cat > /tmp/glitchtip-backup-fixed.sh <<'SCRIPT_EOF'
#!/bin/bash
set -euo pipefail

export PATH="/tools:${PATH}"
export MC_CONFIG_DIR="/tmp/.mc"
export HOME="/tmp"

RUN_STATUS="FAILED"
STATUS_DETAILS="Backup did not complete"
CURRENT_STEP="init"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backups/glitchtip-${TIMESTAMP}"
DB_FILEBLOB_COUNT=0
MIRRORED_OBJECT_COUNT=0
MISSING_COUNT=0
EXTRA_COUNT=0
INTEGRITY_MISMATCH_COUNT=0
PG_DUMP_STATUS="not-started"
PG_DUMP_BYTES=0
VALIDATION_MODE="exact-set-and-integrity-match"

json_escape() {
  sed ':a;N;$!ba;s/\\/\\\\/g;s/"/\\"/g;s/\n/\\n/g' | sed 's/^/"/;s/$/"/'
}

patch_configmap_key() {
  local configmap_name="$1"
  local configmap_key="$2"
  local configmap_value="$3"
  local token cacert namespace payload

  token=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
  cacert=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
  namespace=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo glitchtip)
  payload="{\"data\":{\"${configmap_key}\":${configmap_value}}}"

  curl -sf --cacert "${cacert}" \
    -X PATCH \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/merge-patch+json" \
    -d "${payload}" \
    "https://kubernetes.default.svc/api/v1/namespaces/${namespace}/configmaps/${configmap_name}" >/dev/null
}

report_status() {
  local timestamp_now body
  timestamp_now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  body=$(cat <<EOF | json_escape
# GlitchTip Backup Pipeline Status

## Latest Run
- **Timestamp:** ${timestamp_now}
- **Backup ID:** glitchtip-${TIMESTAMP}
- **Status:** ${RUN_STATUS}

### Details
${STATUS_DETAILS}

### Pipeline Summary
- PostgreSQL dump: ${PG_DUMP_STATUS}
- Database fileblob count: ${DB_FILEBLOB_COUNT}
- Mirrored object count: ${MIRRORED_OBJECT_COUNT}
- Missing objects: ${MISSING_COUNT}
- Extra objects: ${EXTRA_COUNT}
- Integrity mismatches: ${INTEGRITY_MISMATCH_COUNT}
EOF
)

  patch_configmap_key "glitchtip-backup-status" "status.md" "${body}"
}

report_handoff() {
  local timestamp_now result_json reason_json body safe_for_restore blocked
  timestamp_now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  if [ "${RUN_STATUS}" = "SUCCESS" ]; then
    safe_for_restore=true
    blocked=false
    result_json="success"
  else
    safe_for_restore=false
    blocked=true
    result_json="failed"
  fi

  reason_json=$(printf '%s' "${STATUS_DETAILS}" | json_escape)

  body=$(cat <<EOF | json_escape
{
  "latest_run": "${timestamp_now}",
  "backup_id": "glitchtip-${TIMESTAMP}",
  "result": "${result_json}",
  "safe_for_restore": ${safe_for_restore},
  "blocked": ${blocked},
  "pg_dump": {
    "status": "${PG_DUMP_STATUS}",
    "bytes": ${PG_DUMP_BYTES}
  },
  "attachments": {
    "database_fileblobs": ${DB_FILEBLOB_COUNT},
    "mirrored_objects": ${MIRRORED_OBJECT_COUNT}
  },
  "validation": {
    "mode": "${VALIDATION_MODE}",
    "missing_in_object_storage": ${MISSING_COUNT},
    "extra_in_object_storage": ${EXTRA_COUNT},
    "integrity_mismatches": ${INTEGRITY_MISMATCH_COUNT}
  },
  "reason": ${reason_json}
}
EOF
)

  patch_configmap_key "glitchtip-backup-restore-handoff" "handoff.json" "${body}"
}

publish_cluster_records() {
  report_status
  report_handoff
}

fail_backup() {
  RUN_STATUS="FAILED"
  STATUS_DETAILS="$1"
  echo "[backup] VALIDATION FAILED: ${STATUS_DETAILS}"
  publish_cluster_records
  exit 1
}

handle_error() {
  trap - ERR
  if [ "${RUN_STATUS}" != "SUCCESS" ]; then
    STATUS_DETAILS="- Backup failed during step: ${CURRENT_STEP}"
    echo "[backup] ERROR: ${STATUS_DETAILS}"
    publish_cluster_records || true
  fi
  exit 1
}

trap handle_error ERR

mkdir -p "${BACKUP_DIR}/minio-data"

CURRENT_STEP="postgresql-dump"
echo "[backup] Starting GlitchTip backup..."
echo "[backup] Step 1/4: Dumping PostgreSQL database..."
PGPASSWORD="${PGPASSWORD}" pg_dump -h glitchtip-postgresql -U postgres -d postgres \
  --format=custom --file="${BACKUP_DIR}/glitchtip_db.dump"
PG_DUMP_STATUS="success"
PG_DUMP_BYTES=$(wc -c < "${BACKUP_DIR}/glitchtip_db.dump" | tr -d '[:space:]')
echo "[backup] PostgreSQL dump complete: ${BACKUP_DIR}/glitchtip_db.dump"

CURRENT_STEP="mirror-object-storage"
echo "[backup] Step 2/4: Mirroring attachment object storage..."
mc alias set backup "${MINIO_ENDPOINT}" "${MINIO_ACCESS_KEY}" "${MINIO_SECRET_KEY}" --api S3v4 >/dev/null
mc mirror --overwrite "backup/${MINIO_BUCKET}" "${BACKUP_DIR}/minio-data" 2>&1

CURRENT_STEP="build-expected-sets"
PGPASSWORD="${PGPASSWORD}" psql -h glitchtip-postgresql -U postgres -d postgres -At -F $'\t' \
  -c "SELECT blob, size, checksum FROM files_fileblob WHERE blob IS NOT NULL ORDER BY blob;" \
  | sed '/^[[:space:]]*$/d' > "${BACKUP_DIR}/db-metadata.tsv"

cut -f1 "${BACKUP_DIR}/db-metadata.tsv" | sed '/^[[:space:]]*$/d' | sort -u > "${BACKUP_DIR}/db-keys.txt"

find "${BACKUP_DIR}/minio-data" -type f \
  | sed "s#^${BACKUP_DIR}/minio-data/##" \
  | sed '/^[[:space:]]*$/d' | sort -u > "${BACKUP_DIR}/object-keys.txt"

DB_FILEBLOB_COUNT=$(wc -l < "${BACKUP_DIR}/db-keys.txt" | tr -d '[:space:]')
MIRRORED_OBJECT_COUNT=$(wc -l < "${BACKUP_DIR}/object-keys.txt" | tr -d '[:space:]')

CURRENT_STEP="validate-set-drift"
echo "[backup] Step 3/4: Validating database/object-store set alignment..."
comm -23 "${BACKUP_DIR}/db-keys.txt" "${BACKUP_DIR}/object-keys.txt" > "${BACKUP_DIR}/missing-in-object-storage.txt"
comm -13 "${BACKUP_DIR}/db-keys.txt" "${BACKUP_DIR}/object-keys.txt" > "${BACKUP_DIR}/extra-in-object-storage.txt"

MISSING_COUNT=$(wc -l < "${BACKUP_DIR}/missing-in-object-storage.txt" | tr -d '[:space:]')
EXTRA_COUNT=$(wc -l < "${BACKUP_DIR}/extra-in-object-storage.txt" | tr -d '[:space:]')

echo "[backup] Validation counts: pg_fileblobs=${DB_FILEBLOB_COUNT}, mirrored_objects=${MIRRORED_OBJECT_COUNT}"

if [ "${MISSING_COUNT}" -gt 0 ]; then
  MISSING_SAMPLE=$(head -3 "${BACKUP_DIR}/missing-in-object-storage.txt" | paste -sd ', ' -)
  echo "[backup] Validation detail: database references missing from object storage: ${MISSING_SAMPLE}"
fi

if [ "${EXTRA_COUNT}" -gt 0 ]; then
  EXTRA_SAMPLE=$(head -3 "${BACKUP_DIR}/extra-in-object-storage.txt" | paste -sd ', ' -)
  echo "[backup] Validation detail: object storage has extra objects with no database record: ${EXTRA_SAMPLE}"
fi

CURRENT_STEP="validate-content-integrity"
echo "[backup] Step 4/5: Validating mirrored object content integrity..."
: > "${BACKUP_DIR}/integrity-mismatches.txt"

while IFS=$'\t' read -r blob expected_size expected_checksum; do
  [ -z "${blob}" ] && continue
  object_path="${BACKUP_DIR}/minio-data/${blob}"
  if [ ! -f "${object_path}" ]; then
    continue
  fi

  actual_size=$(wc -c < "${object_path}" | tr -d '[:space:]')
  actual_checksum=$(sha1sum "${object_path}" | awk '{print $1}')
  if [ "${actual_size}" != "${expected_size}" ] || [ "${actual_checksum}" != "${expected_checksum}" ]; then
    echo "${blob} checksum mismatch expected=${expected_checksum} actual=${actual_checksum} size_expected=${expected_size} size_actual=${actual_size}" >> "${BACKUP_DIR}/integrity-mismatches.txt"
  fi
done < "${BACKUP_DIR}/db-metadata.tsv"

INTEGRITY_MISMATCH_COUNT=$(wc -l < "${BACKUP_DIR}/integrity-mismatches.txt" | tr -d '[:space:]')

if [ "${INTEGRITY_MISMATCH_COUNT}" -gt 0 ]; then
  INTEGRITY_SAMPLE=$(head -3 "${BACKUP_DIR}/integrity-mismatches.txt" | paste -sd ';' -)
  echo "[backup] Validation detail: integrity mismatches in mirrored content: ${INTEGRITY_SAMPLE}"
fi

if [ "${MISSING_COUNT}" -gt 0 ] || [ "${EXTRA_COUNT}" -gt 0 ] || [ "${INTEGRITY_MISMATCH_COUNT}" -gt 0 ]; then
  fail_backup "- Set drift detected
- missing_in_object_storage=${MISSING_COUNT}
- extra_in_object_storage=${EXTRA_COUNT}
- integrity_mismatches=${INTEGRITY_MISMATCH_COUNT}
- pg_fileblobs=${DB_FILEBLOB_COUNT}
- mirrored_objects=${MIRRORED_OBJECT_COUNT}"
fi

CURRENT_STEP="record-manifest"
echo "[backup] Step 5/5: Recording backup metadata..."
cat > "${BACKUP_DIR}/backup_manifest.txt" <<EOF
timestamp=${TIMESTAMP}
pg_dump=success
pg_fileblobs=${DB_FILEBLOB_COUNT}
mirrored_objects=${MIRRORED_OBJECT_COUNT}
missing_in_object_storage=${MISSING_COUNT}
extra_in_object_storage=${EXTRA_COUNT}
integrity_mismatches=${INTEGRITY_MISMATCH_COUNT}
validation=exact-set-and-integrity-match
EOF

RUN_STATUS="SUCCESS"
STATUS_DETAILS="- PostgreSQL dump: success
- Validation: exact set and content-integrity comparison passed
- pg_fileblobs=${DB_FILEBLOB_COUNT}
- mirrored_objects=${MIRRORED_OBJECT_COUNT}
- missing_in_object_storage=0
- extra_in_object_storage=0
- integrity_mismatches=0"
publish_cluster_records

echo "[backup] Validation passed: exact database/object-store sets and object bytes match"
echo "[backup] Backup complete."
SCRIPT_EOF

echo "[solution] Creating status-reporting service account and RBAC..."
kubectl apply -f - <<'RBAC_EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: glitchtip-backup-sa
  namespace: glitchtip
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: backup-status-writer
  namespace: glitchtip
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  resourceNames: ["glitchtip-backup-status", "glitchtip-backup-restore-handoff"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: backup-status-writer-binding
  namespace: glitchtip
subjects:
- kind: ServiceAccount
  name: glitchtip-backup-sa
  namespace: glitchtip
roleRef:
  kind: Role
  name: backup-status-writer
  apiGroup: rbac.authorization.k8s.io
RBAC_EOF

echo "[solution] Updating both the active and approved backup scripts..."
kubectl create configmap glitchtip-backup-script \
  --from-file=backup.sh=/tmp/glitchtip-backup-fixed.sh \
  -n glitchtip \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap glitchtip-backup-script-original \
  --from-file=backup.sh=/tmp/glitchtip-backup-fixed.sh \
  -n glitchtip \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[solution] Updating the approved runtime baseline to use the dedicated status-reporting service account..."
kubectl create configmap glitchtip-backup-runtime-original \
  --from-literal=serviceAccountName=glitchtip-backup-sa \
  -n glitchtip \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[solution] Updating the CronJob to use the status-reporting service account..."
kubectl apply -f - <<'CRONJOB_EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: glitchtip-backup
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup
  annotations:
    description: "Automated GlitchTip backup — runs daily at 2 AM"
    created-by: "platform-team"
    last-reviewed: "2026-04-04"
spec:
  schedule: "0 2 * * *"
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 600
      template:
        metadata:
          labels:
            app: glitchtip
            job: backup
        spec:
          serviceAccountName: glitchtip-backup-sa
          restartPolicy: Never
          volumes:
          - name: backup-script
            configMap:
              name: glitchtip-backup-script
              defaultMode: 0755
          - name: backup-storage
            emptyDir: {}
          - name: tools
            emptyDir: {}
          initContainers:
          - name: copy-tools
            image: docker.io/minio/minio:latest
            imagePullPolicy: IfNotPresent
            command:
            - /bin/sh
            - -c
            - |
              cp /usr/bin/mc /tools/mc
              cp /usr/bin/curl /tools/curl
              chmod +x /tools/mc /tools/curl
            volumeMounts:
            - name: tools
              mountPath: /tools
          containers:
          - name: backup
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command: ["/bin/bash", "/scripts/backup.sh"]
            volumeMounts:
            - name: backup-script
              mountPath: /scripts
            - name: backup-storage
              mountPath: /backups
            - name: tools
              mountPath: /tools
            env:
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: glitchtip-postgresql
                  key: postgres-password
            - name: MINIO_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: glitchtip-minio-creds
                  key: MINIO_ACCESS_KEY
            - name: MINIO_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: glitchtip-minio-creds
                  key: MINIO_SECRET_KEY
            - name: MINIO_ENDPOINT
              value: "http://glitchtip-minio:9000"
            - name: MINIO_BUCKET
              value: "glitchtip-attachments"
CRONJOB_EOF

echo "[solution] Reference fix applied."
