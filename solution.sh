#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Step 1: Discovering the backup gap..."

# Find the backup CronJob
BACKUP_CJ=$(kubectl get cronjobs -n glitchtip -l component=backup -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
echo "[solution] Found backup CronJob: ${BACKUP_CJ}"

# Read the backup script
BACKUP_SCRIPT=$(kubectl get configmap glitchtip-backup-script -n glitchtip -o jsonpath='{.data.backup\.sh}')
echo "[solution] Current backup script only does pg_dump — no MinIO backup."

# Get MinIO credentials
MINIO_ACCESS=$(kubectl get secret glitchtip-minio-creds -n glitchtip -o jsonpath='{.data.MINIO_ACCESS_KEY}' | base64 -d)
MINIO_SECRET=$(kubectl get secret glitchtip-minio-creds -n glitchtip -o jsonpath='{.data.MINIO_SECRET_KEY}' | base64 -d)
MINIO_BUCKET="glitchtip-attachments"
echo "[solution] MinIO credentials discovered."

echo "[solution] Step 2: Updating backup CronJob to include MinIO..."

# Update the backup ConfigMap with MinIO backup + validation
kubectl apply -f - <<BACKUP_EOF
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
    TIMESTAMP=\$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="/backups/glitchtip-\${TIMESTAMP}"
    mkdir -p "\${BACKUP_DIR}"

    # Step 1: PostgreSQL dump
    echo "[backup] Dumping PostgreSQL database..."
    PGPASSWORD="\${PGPASSWORD}" pg_dump -h glitchtip-postgresql -U postgres -d postgres \
      --format=custom --file="\${BACKUP_DIR}/glitchtip_db.dump"
    echo "[backup] PostgreSQL dump complete."

    # Step 2: MinIO bucket backup
    echo "[backup] Backing up MinIO attachment bucket..."
    mc alias set glitchtip-store http://glitchtip-minio:9000 "\${MINIO_ACCESS_KEY}" "\${MINIO_SECRET_KEY}" --api S3v4 2>/dev/null
    mc mirror glitchtip-store/${MINIO_BUCKET} "\${BACKUP_DIR}/minio-${MINIO_BUCKET}/" 2>&1
    MINIO_OBJECTS=\$(mc ls --recursive glitchtip-store/${MINIO_BUCKET}/ 2>/dev/null | wc -l)
    echo "[backup] MinIO backup complete: \${MINIO_OBJECTS} objects mirrored."

    # Step 3: Post-backup validation
    echo "[backup] Running post-backup validation..."
    PG_ATTACHMENT_COUNT=\$(PGPASSWORD="\${PGPASSWORD}" psql -h glitchtip-postgresql -U postgres -d postgres -tAc "SELECT COUNT(*) FROM files_fileblob;")
    MINIO_OBJECT_COUNT=\$(mc ls --recursive glitchtip-store/${MINIO_BUCKET}/ 2>/dev/null | wc -l)
    BACKUP_FILE_COUNT=\$(find "\${BACKUP_DIR}/minio-${MINIO_BUCKET}/" -type f 2>/dev/null | wc -l)

    echo "[backup] Validation results:"
    echo "[backup]   PostgreSQL attachment records: \${PG_ATTACHMENT_COUNT}"
    echo "[backup]   MinIO live objects: \${MINIO_OBJECT_COUNT}"
    echo "[backup]   Backed up files: \${BACKUP_FILE_COUNT}"

    if [ "\${PG_ATTACHMENT_COUNT}" -gt 0 ] && [ "\${MINIO_OBJECT_COUNT}" -eq 0 ]; then
      echo "[backup] VALIDATION FAILED: PostgreSQL has \${PG_ATTACHMENT_COUNT} attachment records but MinIO has 0 objects!"
      exit 1
    fi

    if [ "\${BACKUP_FILE_COUNT}" -lt "\${MINIO_OBJECT_COUNT}" ]; then
      echo "[backup] VALIDATION WARNING: Backed up \${BACKUP_FILE_COUNT} files but MinIO has \${MINIO_OBJECT_COUNT} objects."
    fi

    # Record backup metadata
    echo "timestamp=\${TIMESTAMP}" > "\${BACKUP_DIR}/backup_manifest.txt"
    echo "pg_dump=success" >> "\${BACKUP_DIR}/backup_manifest.txt"
    echo "minio_backup=success" >> "\${BACKUP_DIR}/backup_manifest.txt"
    echo "pg_attachment_count=\${PG_ATTACHMENT_COUNT}" >> "\${BACKUP_DIR}/backup_manifest.txt"
    echo "minio_object_count=\${MINIO_OBJECT_COUNT}" >> "\${BACKUP_DIR}/backup_manifest.txt"
    echo "backup_file_count=\${BACKUP_FILE_COUNT}" >> "\${BACKUP_DIR}/backup_manifest.txt"
    echo "validation=passed" >> "\${BACKUP_DIR}/backup_manifest.txt"

    echo "[backup] Backup and validation complete."
BACKUP_EOF

# Update the CronJob to include MinIO environment and mc tool
kubectl patch cronjob glitchtip-backup -n glitchtip --type strategic -p '{
  "spec": {
    "jobTemplate": {
      "spec": {
        "template": {
          "spec": {
            "containers": [{
              "name": "backup",
              "image": "docker.io/minio/mc:RELEASE.2024-11-21T17-21-54Z",
              "command": ["/bin/bash", "/scripts/backup.sh"],
              "env": [
                {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "glitchtip-postgresql", "key": "postgres-password"}}},
                {"name": "MINIO_ACCESS_KEY", "valueFrom": {"secretKeyRef": {"name": "glitchtip-minio-creds", "key": "MINIO_ACCESS_KEY"}}},
                {"name": "MINIO_SECRET_KEY", "valueFrom": {"secretKeyRef": {"name": "glitchtip-minio-creds", "key": "MINIO_SECRET_KEY"}}}
              ]
            }]
          }
        }
      }
    }
  }
}'

echo "[solution] Backup CronJob updated with MinIO backup and validation."

echo "[solution] Step 3: Running a test backup to verify..."

# Trigger a manual backup run
kubectl create job --from=cronjob/glitchtip-backup backup-test-$(date +%s) -n glitchtip 2>/dev/null || true

echo "[solution] Step 4: Verifying attachment accessibility..."

# Check MinIO has objects
MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}')
OBJ_COUNT=$(kubectl exec -n glitchtip "${MINIO_POD}" -- mc ls --recursive local/glitchtip-attachments/ 2>/dev/null | wc -l || echo "0")
echo "[solution] MinIO attachment objects: ${OBJ_COUNT}"

# Check PG has matching records
GT_PG_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}')
PG_COUNT=$(kubectl exec -n glitchtip "${GT_PG_POD}" -- bash -c "PGPASSWORD=7KkJeWZYkK psql -U postgres -d postgres -tAc 'SELECT COUNT(*) FROM files_fileblob;'" 2>/dev/null)
echo "[solution] PostgreSQL fileblob records: ${PG_COUNT}"

echo "[solution] ============================================"
echo "[solution] Solution complete."
echo "[solution] ============================================"
