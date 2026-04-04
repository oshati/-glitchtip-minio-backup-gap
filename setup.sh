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
# MOVE MINIO CREDS TO SECRETREF FIRST (before creating objects)
# This causes a restart — must be done before populating data
###############################################
echo "[setup] Moving MinIO creds to secretRef..."
kubectl patch deployment glitchtip-minio -n glitchtip --type json -p '[
  {"op": "replace", "path": "/spec/template/spec/containers/0/env/0", "value": {"name": "MINIO_ROOT_USER", "valueFrom": {"secretKeyRef": {"name": "glitchtip-minio-creds", "key": "MINIO_ACCESS_KEY"}}}},
  {"op": "replace", "path": "/spec/template/spec/containers/0/env/1", "value": {"name": "MINIO_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "glitchtip-minio-creds", "key": "MINIO_SECRET_KEY"}}}}
]' 2>/dev/null || true
kubectl rollout status deployment/glitchtip-minio -n glitchtip --timeout=120s || true

# Wait for MinIO to come back after secretRef restart
for i in $(seq 1 30); do
  MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -n "${MINIO_POD}" ] && kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "mc alias set local http://localhost:9000 '${MINIO_ACCESS_KEY}' '${MINIO_SECRET_KEY}' >/dev/null 2>&1 && mc ls local/ >/dev/null 2>&1" 2>/dev/null; then
    echo "[setup] MinIO back with secretRef creds."
    break
  fi
  sleep 5
done

###############################################
# CREATE BUCKET AND POPULATE WITH ATTACHMENTS
###############################################
echo "[setup] Creating attachment bucket and sample files..."

LIVE_MISSING_BLOB="attachments/event_3/crash_report_3.dmp"
LIVE_STALE_BLOB="attachments/drift/live_extra_trace.bin"
INTEGRITY_TARGET_BLOB="attachments/event_6/crash_report_6.dmp"

randhex() {
  head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n'
}

seed_blob_and_metadata() {
  local blob_path="$1"
  local content="$2"
  local size checksum

  size=$(printf "%s" "${content}" | wc -c | tr -d '[:space:]')
  checksum=$(printf "%s" "${content}" | sha1sum | awk '{print $1}')

  kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "
    printf '%s' '${content}' | mc pipe local/${MINIO_BUCKET}/${blob_path} >/dev/null 2>&1
  " 2>/dev/null

  gt_sql "INSERT INTO files_fileblob (created, checksum, size, blob)
    VALUES (NOW(), '${checksum}', ${size}, '${blob_path}')
    ON CONFLICT (checksum) DO NOTHING;"
}

MINIO_POD=$(kubectl get pods -n glitchtip -l app=glitchtip-minio -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "
mc alias set local http://localhost:9000 '${MINIO_ACCESS_KEY}' '${MINIO_SECRET_KEY}' >/dev/null 2>&1
mc mb local/${MINIO_BUCKET} 2>/dev/null || true
" 2>&1

LIVE_MISSING_CONTENT=""
LIVE_STALE_CONTENT="stale$(randhex)"
INTEGRITY_TARGET_CONTENT=""
INTEGRITY_CORRUPTED_CONTENT="corrupt$(randhex)"

for i in $(seq 1 10); do
  CRASH_PATH="attachments/event_${i}/crash_report_${i}.dmp"
  SOURCE_PATH="attachments/event_${i}/sourcemap_${i}.js.map"
  CRASH_CONTENT="crash$(randhex)"
  SOURCE_CONTENT="smap$(randhex)"

  if [ "${CRASH_PATH}" = "${LIVE_MISSING_BLOB}" ]; then
    LIVE_MISSING_CONTENT="${CRASH_CONTENT}"
  fi

  if [ "${CRASH_PATH}" = "${INTEGRITY_TARGET_BLOB}" ]; then
    INTEGRITY_TARGET_CONTENT="${CRASH_CONTENT}"
    while [ "${INTEGRITY_CORRUPTED_CONTENT}" = "${INTEGRITY_TARGET_CONTENT}" ]; do
      INTEGRITY_CORRUPTED_CONTENT="corrupt$(randhex)"
    done
  fi

  seed_blob_and_metadata "${CRASH_PATH}" "${CRASH_CONTENT}"
  seed_blob_and_metadata "${SOURCE_PATH}" "${SOURCE_CONTENT}"
done

for i in $(seq 1 5); do
  DSYM_PATH="debug-symbols/dsym_${i}.dSYM"
  DSYM_CONTENT="dsym$(randhex)"
  seed_blob_and_metadata "${DSYM_PATH}" "${DSYM_CONTENT}"
done

# Verify
OBJ_COUNT=$(kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "mc ls --recursive local/${MINIO_BUCKET}/ 2>/dev/null | wc -l")
echo "[setup] MinIO objects created: ${OBJ_COUNT}"

###############################################
# INSERT FILE RECORDS IN POSTGRESQL
###############################################
echo "[setup] Inserting attachment records in PostgreSQL..."

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
# INJECT LIVE BALANCED DRIFT
# One DB-referenced object is missing and one
# extra object exists only in MinIO. Counts still
# match, so naive validation can report success.
###############################################
echo "[setup] Injecting balanced attachment drift..."
kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "
mc alias set local http://localhost:9000 '${MINIO_ACCESS_KEY}' '${MINIO_SECRET_KEY}' >/dev/null 2>&1
mc rm --force local/${MINIO_BUCKET}/${LIVE_MISSING_BLOB} >/dev/null 2>&1 || true
printf '%s' '${LIVE_STALE_CONTENT}' | mc pipe local/${MINIO_BUCKET}/${LIVE_STALE_BLOB} >/dev/null 2>&1
" 2>/dev/null

OBJ_COUNT=$(kubectl exec -n glitchtip "${MINIO_POD}" -- sh -c "mc ls --recursive local/${MINIO_BUCKET}/ 2>/dev/null | wc -l")
echo "[setup] Live object-store count after drift injection: ${OBJ_COUNT}"

###############################################
# CREATE PARTIALLY MIGRATED BACKUP CRONJOB
# The pipeline mirrors MinIO and records counts,
# but only validates total counts and does not
# update the persistent status surface.
###############################################
echo "[setup] Creating partially migrated backup CronJob..."

kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-backup-script
  namespace: glitchtip
data:
  backup.sh: |
    #!/bin/bash
    set -euo pipefail
    export PATH="/tools:${PATH}"
    export MC_CONFIG_DIR="/tmp/.mc"
    export HOME="/tmp"

    echo "[backup] Starting GlitchTip backup..."
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="/backups/glitchtip-${TIMESTAMP}"
    mkdir -p "${BACKUP_DIR}/minio-data"

    # Step 1: PostgreSQL dump
    echo "[backup] Step 1/3: Dumping PostgreSQL database..."
    PGPASSWORD="${PGPASSWORD}" pg_dump -h glitchtip-postgresql -U postgres -d postgres \
      --format=custom --file="${BACKUP_DIR}/glitchtip_db.dump"
    echo "[backup] PostgreSQL dump complete: ${BACKUP_DIR}/glitchtip_db.dump"

    # Step 2: Mirror object storage
    echo "[backup] Step 2/3: Mirroring attachment object storage..."
    mc alias set backup "${MINIO_ENDPOINT}" "${MINIO_ACCESS_KEY}" "${MINIO_SECRET_KEY}" --api S3v4 >/dev/null
    mc mirror --overwrite "backup/${MINIO_BUCKET}" "${BACKUP_DIR}/minio-data" 2>&1

    # Step 3: Count-based validation only
    echo "[backup] Step 3/3: Running backup validation..."
    PG_BLOB_COUNT=$(PGPASSWORD="${PGPASSWORD}" psql -h glitchtip-postgresql -U postgres -d postgres -tAc "SELECT COUNT(*) FROM files_fileblob;" | tr -d '[:space:]')
    MINIO_OBJECT_COUNT=$(find "${BACKUP_DIR}/minio-data" -type f | wc -l | tr -d '[:space:]')
    echo "[backup] Validation counts: pg_fileblobs=${PG_BLOB_COUNT}, mirrored_objects=${MINIO_OBJECT_COUNT}"

    if [ "${PG_BLOB_COUNT}" != "${MINIO_OBJECT_COUNT}" ]; then
      echo "[backup] VALIDATION FAILED: backup counts do not match"
      exit 1
    fi
    echo "[backup] Validation passed: mirrored object count matches database fileblob count"

    echo "[backup] Recording backup metadata..."
    echo "timestamp=${TIMESTAMP}" > "${BACKUP_DIR}/backup_manifest.txt"
    echo "pg_dump=success" >> "${BACKUP_DIR}/backup_manifest.txt"
    echo "pg_fileblobs=${PG_BLOB_COUNT}" >> "${BACKUP_DIR}/backup_manifest.txt"
    echo "mirrored_objects=${MINIO_OBJECT_COUNT}" >> "${BACKUP_DIR}/backup_manifest.txt"
    echo "validation=count-match-only" >> "${BACKUP_DIR}/backup_manifest.txt"

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
EOF

###############################################
# ACCESS POLICY: KEEP MINIO SCOPED TO AUTHORIZED
# WORKLOADS ONLY
###############################################
echo "[setup] Creating scoped NetworkPolicy for MinIO..."

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
  # Allow from GlitchTip web pods
  - from:
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: glitchtip
    ports:
    - protocol: TCP
      port: 9000
  # Allow from the backup pipeline only
  - from:
    - podSelector:
        matchLabels:
          app: glitchtip
          job: backup
    ports:
    - protocol: TCP
      port: 9000
EOF

###############################################
# ENFORCER: CRONJOB THAT REVERTS BACKUP SCRIPT
# Runs every minute, overwrites glitchtip-backup-script
# ConfigMap back to the approved count-based version.
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
- apiGroups: ["batch"]
  resources: ["cronjobs"]
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
  schedule: "*/1 * * * *"
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

###############################################
# RUNTIME ENFORCER: CRONJOB THAT RECONCILES
# THE APPROVED SERVICE ACCOUNT FOR THE BACKUP
# CRONJOB. One-off serviceAccountName edits
# will drift back unless the approved runtime
# baseline is updated too.
###############################################
echo "[setup] Creating backup runtime manager..."

kubectl create configmap glitchtip-backup-runtime-original -n glitchtip \
  --from-literal="serviceAccountName=default" \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: glitchtip-backup-template-manager
  namespace: glitchtip
  labels:
    app: glitchtip
    component: backup-template
  annotations:
    description: "Reconciles approved backup job runtime settings"
spec:
  schedule: "*/1 * * * *"
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
            job: backup-template
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
              APPROVED_SA=$(tr -d '[:space:]' < /approved/serviceAccountName 2>/dev/null)
              if [ -z "${APPROVED_SA}" ]; then
                APPROVED_SA="default"
              fi
              PATCH="{\"spec\":{\"jobTemplate\":{\"spec\":{\"template\":{\"spec\":{\"serviceAccountName\":\"${APPROVED_SA}\"}}}}}}"
              TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
              CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
              curl -sf --cacert "${CACERT}" \
                -X PATCH \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Content-Type: application/merge-patch+json" \
                -d "${PATCH}" \
                "https://kubernetes.default.svc/apis/batch/v1/namespaces/glitchtip/cronjobs/glitchtip-backup" >/dev/null 2>&1 || true
              echo "Runtime policy reconciliation complete."
            volumeMounts:
            - name: approved-runtime
              mountPath: /approved
          volumes:
          - name: approved-runtime
            configMap:
              name: glitchtip-backup-runtime-original
              optional: true
EOF

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
    ## Latest Recorded Run: 2026-03-30T02:00:14Z

    ### Reported Result
    - Status: SUCCESS
    - Backup ID: glitchtip-20260330_020014
    - Validation summary: database fileblob count matched mirrored object count

    ### Current State
    - Backup CronJob: glitchtip-backup (runs daily at 2 AM)
    - Backup type: PostgreSQL custom-format dump + MinIO mirror
    - Validation mode: count comparison
    - Storage: emptyDir (backup files are ephemeral — not persisted to PVC)
    - Retention: 3 successful job history

    ### Recent Changes
    - Q4 2024: Fixed TLS certificate issue (see incident report)
    - Q4 2024: Added backup manifest with object counts
    - Q1 2025: MinIO mirror step added to backup pipeline

    ### Known Issues
    - Backup storage is ephemeral (emptyDir) — backups are lost when pod terminates
    - Status note is only as trustworthy as the backup validation step
    - Ad-hoc backup job template edits have not always survived the next lifecycle reconciliation
    - No off-site backup replication configured
    - Disk space monitoring not configured for backup volume

    ### Contacts
    - Platform team: #platform-eng on Mattermost
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: glitchtip-backup-restore-handoff
  namespace: glitchtip
  labels:
    app: glitchtip
    component: documentation
data:
  handoff.json: |
    {
      "latest_run": "2026-03-30T02:00:14Z",
      "backup_id": "glitchtip-20260330_020014",
      "result": "success",
      "safe_for_restore": true,
      "pg_dump": {
        "status": "success",
        "bytes": 472115
      },
      "attachments": {
        "verified_objects": 25,
        "validation_mode": "count-comparison"
      },
      "notes": [
        "This restore handoff record is stale until the current pipeline refreshes it.",
        "Downstream recovery tooling checks this object before automated drill handoff."
      ]
    }
EOF

###############################################
# STRIP ANNOTATIONS
###############################################
kubectl annotate configmap/glitchtip-backup-script -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-backup-script-original -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-backup-runtime-original -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-dr-incident-2024q3 -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-backup-status -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/glitchtip-backup-restore-handoff -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate cronjob/glitchtip-backup -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate cronjob/glitchtip-backup-retention-manager -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate cronjob/glitchtip-backup-template-manager -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate secret/glitchtip-minio-creds -n glitchtip kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
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
LIVE_MISSING_BLOB=${LIVE_MISSING_BLOB}
LIVE_MISSING_CONTENT=${LIVE_MISSING_CONTENT}
LIVE_STALE_BLOB=${LIVE_STALE_BLOB}
LIVE_STALE_CONTENT=${LIVE_STALE_CONTENT}
INTEGRITY_TARGET_BLOB=${INTEGRITY_TARGET_BLOB}
INTEGRITY_TARGET_CONTENT=${INTEGRITY_TARGET_CONTENT}
INTEGRITY_CORRUPTED_CONTENT=${INTEGRITY_CORRUPTED_CONTENT}
SETUP_EOF
chmod 600 /root/.setup_info

echo "[setup] ============================================"
echo "[setup] Setup complete. Backup gap established."
echo "[setup] ============================================"
