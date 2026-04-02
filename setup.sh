#!/bin/bash
set -eo pipefail
exec 1> >(stdbuf -oL cat) 2>&1

###############################################
# ENVIRONMENT SETUP
###############################################
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "[setup] Waiting for k3s node to be Ready..."
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 2; done
echo "[setup] k3s is Ready."

mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config

###############################################
# IMPORT CONTAINER IMAGES
###############################################
echo "[setup] Importing container images..."
CTR="ctr --address /run/k3s/containerd/containerd.sock -n k8s.io"
until [ -S /run/k3s/containerd/containerd.sock ]; do sleep 2; done
sleep 5

for img in /var/lib/rancher/k3s/agent/images/*.tar; do
  imgname=$(basename "$img")
  echo "[setup] Importing ${imgname}..."
  for attempt in $(seq 1 5); do
    if $CTR images import "$img" 2>&1; then
      echo "[setup] ${imgname} imported."
      break
    fi
    sleep 10
  done
done

###############################################
# SCALE DOWN NON-ESSENTIAL WORKLOADS
###############################################
echo "[setup] Scaling down non-essential workloads..."
for ns in bleater monitoring observability harbor argocd mattermost; do
  for dep in $(kubectl get deployments -n "$ns" -o name 2>/dev/null); do
    kubectl scale "$dep" -n "$ns" --replicas=0 2>/dev/null || true
  done
  for sts in $(kubectl get statefulsets -n "$ns" -o name 2>/dev/null); do
    kubectl scale "$sts" -n "$ns" --replicas=0 2>/dev/null || true
  done
done

echo "[setup] Waiting for k3s API to stabilize..."
sleep 15
until kubectl get nodes >/dev/null 2>&1; do sleep 5; done
sleep 10
until kubectl get nodes >/dev/null 2>&1; do sleep 3; done
echo "[setup] k3s API stable."

NODE_NAME=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
kubectl taint node "$NODE_NAME" node.kubernetes.io/unreachable- 2>/dev/null || true
kubectl taint node "$NODE_NAME" node.kubernetes.io/not-ready- 2>/dev/null || true
kubectl delete validatingwebhookconfiguration ingress-nginx-admission 2>/dev/null || true

###############################################
# WAIT FOR GLITCHTIP
###############################################
echo "[setup] Waiting for GlitchTip..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=glitchtip -n glitchtip --timeout=300s 2>/dev/null || true

GLITCHTIP_URL="http://glitchtip.devops.local"
for i in $(seq 1 60); do
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GLITCHTIP_URL}" 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "[setup] GlitchTip responding."
    break
  fi
  sleep 5
done

GT_PG_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}')
GT_DB_PASS="7KkJeWZYkK"
GT_DB_USER="postgres"
GT_DB_NAME="postgres"

gt_sql() {
  kubectl exec -n glitchtip "${GT_PG_POD}" -- bash -c "PGPASSWORD=${GT_DB_PASS} psql -U ${GT_DB_USER} -d ${GT_DB_NAME} -tAc \"$1\"" 2>/dev/null
}

# Run migrations
GT_WEB_POD=$(kubectl get pods -n glitchtip -l app.kubernetes.io/name=glitchtip,app.kubernetes.io/component=web -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n glitchtip "${GT_WEB_POD}" -- python manage.py migrate --noinput 2>/dev/null || true

###############################################
# DEPLOY MINIO IN GLITCHTIP NAMESPACE
###############################################
echo "[setup] Deploying MinIO for attachment storage..."

MINIO_ACCESS_KEY="glitchtip-minio"
MINIO_SECRET_KEY="minio-secret-key-2024"
MINIO_BUCKET="glitchtip-attachments"

kubectl apply --validate=false -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: glitchtip-minio-creds
  namespace: glitchtip
type: Opaque
stringData:
  MINIO_ACCESS_KEY: "${MINIO_ACCESS_KEY}"
  MINIO_SECRET_KEY: "${MINIO_SECRET_KEY}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: glitchtip-minio
  namespace: glitchtip
  labels:
    app: glitchtip-minio
spec:
  replicas: 1
  selector:
    matchLabels:
      app: glitchtip-minio
  template:
    metadata:
      labels:
        app: glitchtip-minio
    spec:
      containers:
      - name: minio
        image: docker.io/minio/minio:latest
        imagePullPolicy: IfNotPresent
        args: ["server", "/data", "--console-address", ":9001"]
        ports:
        - containerPort: 9000
        - containerPort: 9001
        env:
        - name: MINIO_ROOT_USER
          value: "${MINIO_ACCESS_KEY}"
        - name: MINIO_ROOT_PASSWORD
          value: "${MINIO_SECRET_KEY}"
---
apiVersion: v1
kind: Service
metadata:
  name: glitchtip-minio
  namespace: glitchtip
spec:
  selector:
    app: glitchtip-minio
  ports:
  - name: api
    port: 9000
    targetPort: 9000
  - name: console
    port: 9001
    targetPort: 9001
EOF

echo "[setup] Waiting for MinIO..."
kubectl rollout status deployment/glitchtip-minio -n glitchtip --timeout=180s || true
kubectl wait --for=condition=ready pod -l app=glitchtip-minio -n glitchtip --timeout=120s

# Wait for MinIO API
MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}')
for i in $(seq 1 30); do
  if kubectl exec -n glitchtip "${MINIO_POD}" -- curl -sf http://localhost:9000/minio/health/live >/dev/null 2>&1; then
    echo "[setup] MinIO healthy."
    break
  fi
  sleep 5
done

###############################################
# CREATE BUCKET AND POPULATE WITH ATTACHMENTS
###############################################
echo "[setup] Creating attachment bucket and sample files..."

# Write the MinIO setup script to a file to avoid quoting issues
# Create objects directly via kubectl exec (no kubectl cp — container lacks tar)
kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "
mc alias set local http://localhost:9000 '${MINIO_ACCESS_KEY}' '${MINIO_SECRET_KEY}' >/dev/null 2>&1
mc mb local/${MINIO_BUCKET} 2>/dev/null || true
" 2>&1

for i in $(seq 1 10); do
  kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "
    echo 'crash-report-data-event-${i}' | mc pipe local/${MINIO_BUCKET}/attachments/event_${i}/crash_report_${i}.dmp 2>/dev/null
    echo 'source-map-service-${i}' | mc pipe local/${MINIO_BUCKET}/attachments/event_${i}/sourcemap_${i}.js.map 2>/dev/null
  " 2>/dev/null
done

for i in $(seq 1 5); do
  kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "
    echo 'debug-symbol-dsym-${i}' | mc pipe local/${MINIO_BUCKET}/debug-symbols/dsym_${i}.dSYM 2>/dev/null
  " 2>/dev/null
done

# Verify
OBJ_COUNT=$(kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "mc ls --recursive local/${MINIO_BUCKET}/ 2>/dev/null | wc -l")
echo "[setup] MinIO objects created: ${OBJ_COUNT}"

###############################################
# INSERT ATTACHMENT METADATA IN POSTGRESQL
###############################################
echo "[setup] Inserting attachment metadata in PostgreSQL..."

# Create fileblob records pointing to MinIO paths
for i in $(seq 1 10); do
  gt_sql "INSERT INTO files_fileblob (created, checksum, size, blob)
    VALUES (NOW(), 'sha1_crash_${i}_$(head -c 8 /dev/urandom | od -A n -t x1 | tr -d ' \n')', 4096, 'attachments/event_${i}/crash_report_${i}.dmp')
    ON CONFLICT (checksum) DO NOTHING;"
  gt_sql "INSERT INTO files_fileblob (created, checksum, size, blob)
    VALUES (NOW(), 'sha1_smap_${i}_$(head -c 8 /dev/urandom | od -A n -t x1 | tr -d ' \n')', 2048, 'attachments/event_${i}/sourcemap_${i}.js.map')
    ON CONFLICT (checksum) DO NOTHING;"
done

for i in $(seq 1 5); do
  gt_sql "INSERT INTO files_fileblob (created, checksum, size, blob)
    VALUES (NOW(), 'sha1_dsym_${i}_$(head -c 8 /dev/urandom | od -A n -t x1 | tr -d ' \n')', 8192, 'debug-symbols/dsym_${i}.dSYM')
    ON CONFLICT (checksum) DO NOTHING;"
done

# Create file records referencing the blobs
BLOB_IDS=$(gt_sql "SELECT id FROM files_fileblob ORDER BY id DESC LIMIT 25;")
for blob_id in $BLOB_IDS; do
  gt_sql "INSERT INTO files_file (created, name, size, type, blob_id, headers)
    VALUES (NOW(), 'attachment', 4096, 'event.attachment', ${blob_id}, '{\"Content-Type\": \"application/octet-stream\"}')
    ON CONFLICT DO NOTHING;" 2>/dev/null || true
done

TOTAL_BLOBS=$(gt_sql "SELECT COUNT(*) FROM files_fileblob;")
echo "[setup] Created ${TOTAL_BLOBS} fileblob records in PostgreSQL."

###############################################
# CREATE BROKEN BACKUP CRONJOB (PostgreSQL only, NO MinIO)
###############################################
echo "[setup] Creating backup CronJob (PostgreSQL only — missing MinIO)..."

kubectl apply -f - <<'EOF'
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
    echo "[backup] PostgreSQL dump complete: ${BACKUP_DIR}/glitchtip_db.dump"

    # Step 2: Record backup metadata
    echo "[backup] Recording backup metadata..."
    echo "timestamp=${TIMESTAMP}" > "${BACKUP_DIR}/backup_manifest.txt"
    echo "pg_dump=success" >> "${BACKUP_DIR}/backup_manifest.txt"
    echo "pg_tables=$(PGPASSWORD="${PGPASSWORD}" psql -h glitchtip-postgresql -U postgres -d postgres -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';")" >> "${BACKUP_DIR}/backup_manifest.txt"

    echo "[backup] Backup complete. Files:"
    ls -la "${BACKUP_DIR}/"
    echo "[backup] Done."
---
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
    last-reviewed: "2024-09-15"
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
          containers:
          - name: backup
            image: docker.io/library/postgres:16-alpine
            imagePullPolicy: IfNotPresent
            command: ["/bin/bash", "/scripts/backup.sh"]
            volumeMounts:
            - name: backup-script
              mountPath: /scripts
            - name: backup-storage
              mountPath: /backups
            env:
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: glitchtip-postgresql
                  key: postgres-password
EOF

###############################################
# DECOY DOCUMENTATION
###############################################
echo "[setup] Creating documentation ConfigMaps..."

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-migration-log
  namespace: glitchtip
  labels:
    app: glitchtip
    component: documentation
  annotations:
    date: "2024-03-15"
data:
  migration-log.md: |
    # GlitchTip Storage Migration Log
    ## March 2024 — Migrated to MinIO for attachment storage

    ### Changes Made
    - Deployed MinIO in glitchtip namespace (glitchtip-minio service)
    - Updated DEFAULT_FILE_STORAGE to use storages.backends.s3boto3.S3Boto3Storage
    - Configured AWS_STORAGE_BUCKET_NAME=glitchtip-attachments
    - Migrated existing attachments from local volume to MinIO bucket
    - Updated GlitchTip Helm values to include MinIO endpoint config

    ### What Was NOT Updated
    - Backup CronJob (glitchtip-backup) — TODO: add MinIO backup step
    - Monitoring — TODO: add MinIO health check to Prometheus
    - DR runbook — TODO: update with MinIO restore procedure

    ### MinIO Access
    - Endpoint: http://glitchtip-minio:9000
    - Console: http://glitchtip-minio:9001
    - Credentials: see glitchtip-minio-creds secret
    - Bucket: glitchtip-attachments

    ### Ticket References
    - PLAT-1042: Migrate GlitchTip attachments to object storage
    - PLAT-1089: Update backup pipeline for MinIO (OPEN — not started)
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-dr-runbook
  namespace: glitchtip
  labels:
    app: glitchtip
    component: documentation
data:
  dr-runbook.md: |
    # GlitchTip Disaster Recovery Runbook
    ## Last Updated: September 2024

    ### Backup
    The glitchtip-backup CronJob runs daily at 2 AM and creates:
    - PostgreSQL custom-format dump (glitchtip_db.dump)
    - Backup manifest with metadata

    ### Restore Procedure
    1. Scale down glitchtip-web to 0 replicas
    2. Restore PostgreSQL: pg_restore -h glitchtip-postgresql -U postgres -d postgres glitchtip_db.dump
    3. Scale up glitchtip-web
    4. Verify issues and projects load correctly

    ### Known Gaps
    - Attachment storage (MinIO) is NOT included in the backup pipeline
    - Debug symbols stored in MinIO will be lost on restore
    - Ticket PLAT-1089 tracks this gap but is unresolved

    ### Contacts
    - Platform team: #platform-eng on Mattermost
    - On-call: See PagerDuty rotation
EOF

###############################################
# STRIP ANNOTATIONS
###############################################
kubectl annotate configmap/glitchtip-backup-script -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-migration-log -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-dr-runbook -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate cronjob/glitchtip-backup -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true

###############################################
# SAVE SETUP INFO
###############################################
cat > /root/.setup_info <<SETUP_EOF
GT_DB_PASS=${GT_DB_PASS}
GT_DB_USER=${GT_DB_USER}
GT_DB_NAME=${GT_DB_NAME}
MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}
MINIO_SECRET_KEY=${MINIO_SECRET_KEY}
MINIO_BUCKET=${MINIO_BUCKET}
MINIO_ENDPOINT=http://glitchtip-minio:9000
SETUP_EOF
chmod 600 /root/.setup_info

echo "[setup] ============================================"
echo "[setup] Setup complete. Backup gap established."
echo "[setup] ============================================"
