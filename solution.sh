#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Step 1: Discovering the backup gap..."

BACKUP_SCRIPT=$(kubectl get configmap glitchtip-backup-script -n glitchtip -o jsonpath='{.data.backup\.sh}' 2>/dev/null)
echo "[solution] Current backup only does pg_dump — no MinIO backup."

MINIO_ACCESS=$(kubectl get secret glitchtip-minio-creds -n glitchtip -o jsonpath='{.data.MINIO_ACCESS_KEY}' | base64 -d)
MINIO_SECRET=$(kubectl get secret glitchtip-minio-creds -n glitchtip -o jsonpath='{.data.MINIO_SECRET_KEY}' | base64 -d)
MINIO_BUCKET="glitchtip-attachments"

echo "[solution] Step 2: Updating backup script to include MinIO + validation..."

# Update the ConfigMap with the fixed backup script
# Uses pg_dump for PostgreSQL AND mc for MinIO (mc available via init container shared volume)
kubectl apply -f - <<'SCRIPT_EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-backup-script
  namespace: glitchtip
data:
  backup.sh: |
    #!/bin/bash
    set -e
    echo "[backup] Starting GlitchTip backup..."
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="/backups/glitchtip-${TIMESTAMP}"
    mkdir -p "${BACKUP_DIR}"

    # Step 1: PostgreSQL dump
    echo "[backup] Dumping PostgreSQL database..."
    PGPASSWORD="${PGPASSWORD}" pg_dump -h glitchtip-postgresql -U postgres -d postgres \
      --format=custom --file="${BACKUP_DIR}/glitchtip_db.dump"
    echo "[backup] PostgreSQL dump complete."

    # Step 2: MinIO bucket backup (mc was placed by init container)
    echo "[backup] Backing up MinIO attachment bucket..."
    if [ -x /usr/local/bin/mc ]; then
      mc alias set glitchtip-store http://glitchtip-minio:9000 "${MINIO_ACCESS_KEY}" "${MINIO_SECRET_KEY}" --api S3v4 2>/dev/null
      mc mirror glitchtip-store/glitchtip-attachments "${BACKUP_DIR}/minio-attachments/" 2>&1
      MINIO_OBJECTS=$(mc ls --recursive glitchtip-store/glitchtip-attachments/ 2>/dev/null | wc -l)
      echo "[backup] MinIO backup complete: ${MINIO_OBJECTS} objects."
    else
      echo "[backup] WARNING: mc not available, skipping MinIO backup"
      MINIO_OBJECTS=0
    fi

    # Step 3: Post-backup validation
    echo "[backup] Running post-backup validation..."
    PG_COUNT=$(PGPASSWORD="${PGPASSWORD}" psql -h glitchtip-postgresql -U postgres -d postgres -tAc "SELECT COUNT(*) FROM files_fileblob;")
    BACKUP_FILES=$(find "${BACKUP_DIR}/minio-attachments/" -type f 2>/dev/null | wc -l)

    echo "[backup] Validation: PG records=${PG_COUNT}, MinIO objects=${MINIO_OBJECTS:-0}, Backed up=${BACKUP_FILES}"

    if [ "${PG_COUNT}" -gt 0 ] && [ "${MINIO_OBJECTS:-0}" -eq 0 ]; then
      echo "[backup] VALIDATION FAILED: PG has ${PG_COUNT} records but MinIO has 0 objects!"
      exit 1
    fi

    echo "timestamp=${TIMESTAMP}" > "${BACKUP_DIR}/manifest.txt"
    echo "pg_dump=success" >> "${BACKUP_DIR}/manifest.txt"
    echo "minio_backup=success" >> "${BACKUP_DIR}/manifest.txt"
    echo "minio_objects=${MINIO_OBJECTS:-0}" >> "${BACKUP_DIR}/manifest.txt"
    echo "pg_records=${PG_COUNT}" >> "${BACKUP_DIR}/manifest.txt"
    echo "validation=passed" >> "${BACKUP_DIR}/manifest.txt"
    echo "[backup] Backup and validation complete."
  mc-install.sh: |
    #!/bin/sh
    # Copy mc binary to shared volume for the main backup container
    cp /usr/bin/mc /shared/mc 2>/dev/null || cp $(which mc) /shared/mc 2>/dev/null || true
    chmod +x /shared/mc 2>/dev/null || true
    echo "mc binary copied to shared volume"
SCRIPT_EOF

echo "[solution] Step 3: Patching CronJob with init container for mc + MinIO env..."

# Replace the CronJob to add init container (copies mc binary) + MinIO env vars
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
          restartPolicy: Never
          volumes:
          - name: backup-script
            configMap:
              name: glitchtip-backup-script
              defaultMode: 0755
          - name: backup-storage
            emptyDir: {}
          - name: mc-binary
            emptyDir: {}
          initContainers:
          - name: mc-setup
            image: docker.io/minio/mc:RELEASE.2024-11-21T17-21-54Z
            imagePullPolicy: IfNotPresent
            command: ["/bin/sh", "/scripts/mc-install.sh"]
            volumeMounts:
            - name: backup-script
              mountPath: /scripts
            - name: mc-binary
              mountPath: /shared
          containers:
          - name: backup
            image: docker.io/library/postgres:16-alpine
            imagePullPolicy: IfNotPresent
            command:
            - /bin/bash
            - -c
            - "cp /mc-bin/mc /usr/local/bin/mc 2>/dev/null; chmod +x /usr/local/bin/mc 2>/dev/null; /scripts/backup.sh"
            volumeMounts:
            - name: backup-script
              mountPath: /scripts
            - name: backup-storage
              mountPath: /backups
            - name: mc-binary
              mountPath: /mc-bin
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
CJ_EOF

echo "[solution] Step 4: Running test backup..."
kubectl create job --from=cronjob/glitchtip-backup backup-test-$(date +%s) -n glitchtip 2>/dev/null || true

# Wait for the test job
for i in $(seq 1 60); do
  STATUS=$(kubectl get jobs -n glitchtip -o jsonpath='{range .items[*]}{.metadata.name}={.status.succeeded}{"\n"}{end}' 2>/dev/null | grep "backup-test" | head -1)
  if echo "$STATUS" | grep -q "=1"; then
    echo "[solution] Test backup completed successfully."
    break
  fi
  sleep 10
done

echo "[solution] Step 5: Verifying MinIO objects..."
MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}')
OBJ_COUNT=$(kubectl exec -n glitchtip "${MINIO_POD}" -- bash -c "mc alias set local http://localhost:9000 glitchtip-minio minio-secret-key-2024 2>/dev/null; mc ls --recursive local/glitchtip-attachments/ 2>/dev/null | wc -l" || echo "0")
echo "[solution] MinIO objects: ${OBJ_COUNT}"

GT_PG_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}')
PG_COUNT=$(kubectl exec -n glitchtip "${GT_PG_POD}" -- bash -c "PGPASSWORD=7KkJeWZYkK psql -U postgres -d postgres -tAc 'SELECT COUNT(*) FROM files_fileblob;'" 2>/dev/null)
echo "[solution] PG fileblob records: ${PG_COUNT}"

echo "[solution] ============================================"
echo "[solution] Solution complete."
echo "[solution] ============================================"
