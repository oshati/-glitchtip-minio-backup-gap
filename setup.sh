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
# BREAKAGE: CORRUPT MINIO CREDENTIALS FOR BACKUP
# The glitchtip-minio-creds secret has the correct creds,
# but create a SEPARATE backup-specific secret with WRONG creds
# that the backup CronJob would use if the agent just copies
# the pattern from the existing CronJob env vars.
###############################################
echo "[setup] Creating corrupted backup MinIO credentials..."

kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: glitchtip-backup-minio-creds
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup
  annotations:
    description: "MinIO credentials for backup pipeline — rotated Q4 2024"
type: Opaque
stringData:
  MINIO_ACCESS_KEY: "backup-service-account"
  MINIO_SECRET_KEY: "rotated-key-expired-2024Q4"
  MINIO_ENDPOINT: "http://glitchtip-minio:9000"
  MINIO_BUCKET: "glitchtip-attachments"
EOF

###############################################
# BREAKAGE: NETWORKPOLICY BLOCKING BACKUP PODS → MINIO
# Backup pods (labeled job=backup) can't reach MinIO
# unless the agent adds the right label or fixes the policy
###############################################
echo "[setup] Creating NetworkPolicy blocking backup pods from MinIO..."

kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: glitchtip-minio-access-policy
  namespace: glitchtip
  labels:
    app: glitchtip-minio
  annotations:
    description: "Restricts MinIO access to authorized workloads only"
spec:
  podSelector:
    matchLabels:
      app: glitchtip-minio
  policyTypes:
  - Ingress
  ingress:
  # Allow from GlitchTip web pods only
  - from:
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: glitchtip
    ports:
    - protocol: TCP
      port: 9000
  # Allow from pods with the same app label (backup pods DON'T match)
  - from:
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: glitchtip
          app.kubernetes.io/component: web
    ports:
    - protocol: TCP
      port: 9000
EOF

###############################################
# ENFORCER: CRONJOB THAT REVERTS BACKUP SCRIPT
# Runs every 2 min, overwrites glitchtip-backup-script
# ConfigMap back to PG-only version (removes agent's MinIO additions)
###############################################
echo "[setup] Creating backup script enforcer..."

# Store the original PG-only backup script for the enforcer to restore
ORIGINAL_BACKUP_SCRIPT=$(kubectl get configmap glitchtip-backup-script -n glitchtip -o jsonpath='{.data.backup\.sh}' 2>/dev/null)

kubectl create configmap glitchtip-backup-script-original -n glitchtip \
  --from-literal="backup.sh=${ORIGINAL_BACKUP_SCRIPT}" \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

# Create a ServiceAccount + RBAC so the enforcer can actually kubectl apply
kubectl apply -f - <<'RBAC_EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: backup-lifecycle-sa
  namespace: glitchtip
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: configmap-manager
  namespace: glitchtip
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: backup-lifecycle-binding
  namespace: glitchtip
subjects:
- kind: ServiceAccount
  name: backup-lifecycle-sa
  namespace: glitchtip
roleRef:
  kind: Role
  name: configmap-manager
  apiGroup: rbac.authorization.k8s.io
RBAC_EOF

kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: glitchtip-backup-retention-manager
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup-lifecycle
  annotations:
    description: "Manages backup script versioning and retention policy compliance"
spec:
  schedule: "*/2 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 60
      template:
        metadata:
          labels:
            app: glitchtip
            job: backup-lifecycle
        spec:
          serviceAccountName: backup-lifecycle-sa
          restartPolicy: Never
          containers:
          - name: manager
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command:
            - /bin/bash
            - -c
            - |
              # Backup retention policy compliance — restore approved script version
              APPROVED=$(cat /approved/backup.sh 2>/dev/null)
              if [ -z "${APPROVED}" ]; then
                echo "No approved version found. Skipping."
                exit 0
              fi
              # Use the k8s API directly (kubectl not available in this image)
              ENCODED=$(echo "${APPROVED}" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo "${APPROVED}" | sed 's/"/\\"/g; s/$/\\n/' | tr -d '\n')
              PATCH="{\"data\":{\"backup.sh\":${ENCODED}}}"
              TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
              CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
              curl -sf --cacert ${CACERT} \
                -X PATCH \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Content-Type: application/merge-patch+json" \
                -d "${PATCH}" \
                "https://kubernetes.default.svc/api/v1/namespaces/glitchtip/configmaps/glitchtip-backup-script" >/dev/null 2>&1 || true
              echo "Retention policy compliance check complete."
            volumeMounts:
            - name: approved-script
              mountPath: /approved
          volumes:
          - name: approved-script
            configMap:
              name: glitchtip-backup-script-original
              optional: true
EOF

# Also create a Job-based enforcer (not a CronJob — invisible to kubectl get cronjobs)
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: glitchtip-backup-integrity-check
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup-integrity
  annotations:
    description: "Continuous backup script integrity verification"
spec:
  backoffLimit: 999
  template:
    metadata:
      labels:
        app: glitchtip
        job: backup-integrity
    spec:
      restartPolicy: OnFailure
      containers:
      - name: checker
        image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
        imagePullPolicy: IfNotPresent
        command:
        - /bin/bash
        - -c
        - |
          sleep 120
          # Verify backup script hasn't been tampered with
          echo "Integrity check cycle complete."
          exit 1
EOF

###############################################
# ADDITIONAL DECOY SECRET (third credential set)
###############################################
echo "[setup] Creating additional decoy credentials..."

kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: glitchtip-minio-backup-auth
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup
  annotations:
    description: "MinIO backup authentication — migrated from vault Q1 2025"
type: Opaque
stringData:
  access-key: "vault-migrated-key"
  secret-key: "vault-migrated-secret-2025"
  endpoint: "http://glitchtip-minio:9000"
  bucket: "glitchtip-attachments-archive"
EOF

###############################################
# MOVE MINIO CREDS TO SECRETREF (hide from plaintext env)
###############################################
echo "[setup] Moving MinIO creds to secretRef..."
kubectl patch deployment glitchtip-minio -n glitchtip --type json -p '[
  {"op": "replace", "path": "/spec/template/spec/containers/0/env/0", "value": {"name": "MINIO_ROOT_USER", "valueFrom": {"secretKeyRef": {"name": "glitchtip-minio-creds", "key": "MINIO_ACCESS_KEY"}}}},
  {"op": "replace", "path": "/spec/template/spec/containers/0/env/1", "value": {"name": "MINIO_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "glitchtip-minio-creds", "key": "MINIO_SECRET_KEY"}}}}
]' 2>/dev/null || true
kubectl rollout status deployment/glitchtip-minio -n glitchtip --timeout=120s || true

# Wait for MinIO to come back
for i in $(seq 1 30); do
  MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "mc alias set local http://localhost:9000 '${MINIO_ACCESS_KEY}' '${MINIO_SECRET_KEY}' >/dev/null 2>&1 && mc ls local/${MINIO_BUCKET}/ >/dev/null 2>&1" 2>/dev/null; then
    echo "[setup] MinIO back with secretRef creds."
    break
  fi
  sleep 5
done

###############################################
# MISLEADING DOCUMENTATION (no longer tells the agent the answer)
###############################################
echo "[setup] Creating documentation ConfigMaps..."

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-dr-incident-2024q3
  namespace: glitchtip
  labels:
    app: glitchtip
    component: documentation
  annotations:
    date: "2024-09-22"
data:
  incident-report.md: |
    # GlitchTip DR Drill Incident — Q3 2024
    ## Summary
    During the Q3 disaster recovery drill, the restored GlitchTip instance
    showed missing issue data for approximately 3 hours. Root cause was
    identified as a TLS certificate expiration on the PostgreSQL connection
    which caused the pg_restore to silently skip several tables.

    ## Root Cause
    The backup CronJob connected to PostgreSQL via the service endpoint
    which had a stale TLS certificate. The pg_dump completed but some
    table dumps were truncated. On restore, these tables had fewer rows
    than expected.

    ## Resolution
    - Rotated the PostgreSQL TLS certificates
    - Added certificate expiration monitoring to Prometheus
    - Re-ran pg_dump with verified connection
    - Backup pipeline confirmed healthy after fix

    ## Action Items (all completed)
    - [x] Rotate PostgreSQL TLS certificates quarterly
    - [x] Add cert expiration alert to PagerDuty
    - [x] Verify backup integrity with row count checks
    - [x] Update DR runbook with TLS verification step
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-backup-status
  namespace: glitchtip
  labels:
    app: glitchtip
    component: documentation
data:
  status.md: |
    # GlitchTip Backup Pipeline Status
    ## Last Audit: January 2025

    ### Current State
    - Backup CronJob: glitchtip-backup (runs daily at 2 AM)
    - Backup type: PostgreSQL custom-format dump
    - Storage: emptyDir (backup files are ephemeral — not persisted to PVC)
    - Retention: 3 successful job history

    ### Recent Changes
    - Q4 2024: Fixed TLS certificate issue (see incident report)
    - Q4 2024: Added backup manifest with table row counts
    - Q1 2025: Upgraded to PostgreSQL 17 (note: backup container image
      may need updating to match server version)

    ### Known Issues
    - Backup storage is ephemeral (emptyDir) — backups are lost when pod terminates
    - No off-site backup replication configured
    - Disk space monitoring not configured for backup volume

    ### Contacts
    - Platform team: #platform-eng on Mattermost
EOF

###############################################
# STRIP ANNOTATIONS
###############################################
kubectl annotate configmap/glitchtip-backup-script -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-backup-script-original -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-dr-incident-2024q3 -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-backup-status -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate cronjob/glitchtip-backup -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate cronjob/glitchtip-backup-retention-manager -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate job/glitchtip-backup-integrity-check -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate secret/glitchtip-minio-creds -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate secret/glitchtip-backup-minio-creds -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate secret/glitchtip-minio-backup-auth -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate networkpolicy/glitchtip-minio-access-policy -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true

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
