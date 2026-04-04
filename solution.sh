#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Step 0: Stopping enforcers that revert backup script..."

# Stop the CronJob enforcer
kubectl patch cronjob glitchtip-backup-retention-manager -n glitchtip -p '{"spec":{"suspend":true}}' 2>/dev/null || true
# Kill any running enforcer Jobs
for job in $(kubectl get jobs -n glitchtip -l job=backup-lifecycle -o name 2>/dev/null); do
  kubectl delete "$job" -n glitchtip --grace-period=0 2>/dev/null || true
done
# Stop the Job-based enforcer
kubectl delete job glitchtip-backup-integrity-check -n glitchtip --grace-period=0 2>/dev/null || true
# Delete the original script ConfigMap so the enforcer has nothing to restore
kubectl delete configmap glitchtip-backup-script-original -n glitchtip 2>/dev/null || true
echo "[solution] Enforcers stopped."

echo "[solution] Step 1: Discovering the backup gap..."

BACKUP_SCRIPT=$(kubectl get configmap glitchtip-backup-script -n glitchtip -o jsonpath='{.data.backup\.sh}' 2>/dev/null)
echo "[solution] Current backup only does pg_dump — no MinIO backup."

MINIO_ACCESS=$(kubectl get secret glitchtip-minio-creds -n glitchtip -o jsonpath='{.data.MINIO_ACCESS_KEY}' | base64 -d)
MINIO_SECRET=$(kubectl get secret glitchtip-minio-creds -n glitchtip -o jsonpath='{.data.MINIO_SECRET_KEY}' | base64 -d)

echo "[solution] Step 1b: Fixing MinIO access for backup pods..."
kubectl delete networkpolicy glitchtip-minio-access-policy -n glitchtip 2>/dev/null || true
echo "[solution] MinIO NetworkPolicy removed."

echo "[solution] Step 2: Updating backup ConfigMap with MinIO + validation..."

kubectl apply -f - <<'SCRIPT_EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-backup-script
  namespace: glitchtip
data:
  backup.sh: |
    #!/bin/sh
    BACKUP_STATUS="success"
    BACKUP_MSG=""
    trap 'report_status' EXIT

    report_status() {
      # Write backup status to a ConfigMap for monitoring
      TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null)
      CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
      TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      PATCH="{\"data\":{\"last-run\":\"${TIMESTAMP}\",\"last-status\":\"${BACKUP_STATUS}\",\"last-message\":\"${BACKUP_MSG}\"}}"
      if [ -n "${TOKEN}" ]; then
        curl -sf --cacert ${CACERT} \
          -X PATCH \
          -H "Authorization: Bearer ${TOKEN}" \
          -H "Content-Type: application/merge-patch+json" \
          -d "${PATCH}" \
          "https://kubernetes.default.svc/api/v1/namespaces/glitchtip/configmaps/glitchtip-backup-status" >/dev/null 2>&1 || true
      fi
      echo "[backup] status=${BACKUP_STATUS} result=backup_status=${BACKUP_STATUS} message=${BACKUP_MSG}"
    }

    echo "[backup] Starting GlitchTip backup..."

    # PostgreSQL dump done by init container
    if [ -f /backups/glitchtip_db.dump ]; then
      echo "[backup] PostgreSQL dump verified."
    else
      echo "[backup] WARNING: No PostgreSQL dump found"
    fi

    # MinIO bucket backup
    echo "[backup] Backing up MinIO attachment bucket..."
    if ! mc alias set glitchtip-store http://glitchtip-minio:9000 "${MINIO_ACCESS_KEY}" "${MINIO_SECRET_KEY}" >/dev/null 2>&1; then
      BACKUP_STATUS="failed"
      BACKUP_MSG="MinIO alias setup failed — connection or credentials error"
      echo "[backup] VALIDATION FAILED: ${BACKUP_MSG}"
      exit 1
    fi

    mc mirror glitchtip-store/glitchtip-attachments /backups/minio-attachments/ 2>&1
    MINIO_OBJECTS=$(mc ls --recursive glitchtip-store/glitchtip-attachments/ 2>/dev/null | wc -l)
    echo "[backup] MinIO backup complete: ${MINIO_OBJECTS} objects mirrored."

    # Validation — compare PG fileblob count (from init container) against MinIO object count
    echo "[backup] Running post-backup validation..."
    PG_COUNT=$(cat /backups/.pg_fileblob_count 2>/dev/null | tr -d '[:space:]')
    PG_COUNT=${PG_COUNT:-0}
    echo "[backup] Validation: PG fileblob records=${PG_COUNT}, MinIO objects=${MINIO_OBJECTS}"

    if [ "${PG_COUNT}" -gt "${MINIO_OBJECTS}" ]; then
      DIFF=$((PG_COUNT - MINIO_OBJECTS))
      BACKUP_STATUS="failed"
      BACKUP_MSG="PG has ${PG_COUNT} records but MinIO only has ${MINIO_OBJECTS} objects — ${DIFF} missing"
      echo "[backup] VALIDATION FAILED: ${BACKUP_MSG}"
      exit 1
    fi

    if [ "${MINIO_OBJECTS}" -eq 0 ]; then
      BACKUP_STATUS="failed"
      BACKUP_MSG="MinIO has 0 objects — bucket may be empty or unreachable"
      echo "[backup] VALIDATION FAILED: ${BACKUP_MSG}"
      exit 1
    fi

    BACKUP_MSG="PG=${PG_COUNT}, MinIO=${MINIO_OBJECTS} — consistent"
    echo "[backup] Backup and validation complete. ${BACKUP_MSG}"
SCRIPT_EOF

echo "[solution] Step 2b: Creating ServiceAccount for backup status reporting..."

kubectl apply -f - <<'SA_EOF'
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
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: backup-status-binding
  namespace: glitchtip
subjects:
- kind: ServiceAccount
  name: glitchtip-backup-sa
  namespace: glitchtip
roleRef:
  kind: Role
  name: backup-status-writer
  apiGroup: rbac.authorization.k8s.io
SA_EOF

echo "[solution] Step 3: Updating CronJob with pg17 init + mc main container..."

kubectl apply -f - <<'CJ_EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: glitchtip-backup
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup
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
          initContainers:
          - name: pg-dump
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command:
            - /bin/bash
            - -c
            - |
              echo "[backup] Dumping PostgreSQL database..."
              PGPASSWORD="${PGPASSWORD}" pg_dump -h glitchtip-postgresql -U postgres -d postgres \
                --format=custom --file="/backups/glitchtip_db.dump"
              echo "[backup] PostgreSQL dump complete."
              # Write PG fileblob count for validation by main container
              PGPASSWORD="${PGPASSWORD}" psql -h glitchtip-postgresql -U postgres -d postgres \
                -tAc "SELECT COUNT(*) FROM files_fileblob;" > /backups/.pg_fileblob_count 2>/dev/null || echo "0" > /backups/.pg_fileblob_count
              echo "[backup] PG fileblob count: $(cat /backups/.pg_fileblob_count)"
            volumeMounts:
            - name: backup-storage
              mountPath: /backups
            env:
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: glitchtip-postgresql
                  key: postgres-password
          containers:
          - name: backup
            image: docker.io/minio/mc:RELEASE.2024-11-21T17-21-54Z
            imagePullPolicy: IfNotPresent
            command: ["/bin/sh", "/scripts/backup.sh"]
            volumeMounts:
            - name: backup-script
              mountPath: /scripts
            - name: backup-storage
              mountPath: /backups
            env:
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
CJ_EOF

echo "[solution] Step 4: Verifying MinIO objects..."
MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}')
OBJ_COUNT=$(kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "mc alias set local http://localhost:9000 ${MINIO_ACCESS} ${MINIO_SECRET} >/dev/null 2>&1; mc ls --recursive local/glitchtip-attachments/ 2>/dev/null | wc -l")
echo "[solution] MinIO objects: ${OBJ_COUNT}"

GT_PG_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}')
PG_COUNT=$(kubectl exec -n glitchtip "${GT_PG_POD}" -- bash -c "PGPASSWORD=7KkJeWZYkK psql -U postgres -d postgres -tAc 'SELECT COUNT(*) FROM files_fileblob;'" 2>/dev/null)
echo "[solution] PG fileblob records: ${PG_COUNT}"

echo "[solution] ============================================"
echo "[solution] Solution complete."
echo "[solution] ============================================"
