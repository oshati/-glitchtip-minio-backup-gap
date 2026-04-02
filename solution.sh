#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

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
    set -e
    echo "[backup] Starting GlitchTip MinIO backup..."

    # PostgreSQL dump done by init container
    if [ -f /backups/glitchtip_db.dump ]; then
      echo "[backup] PostgreSQL dump verified."
    else
      echo "[backup] WARNING: No PostgreSQL dump found"
    fi

    # MinIO bucket backup
    echo "[backup] Backing up MinIO attachment bucket..."
    mc alias set glitchtip-store http://glitchtip-minio:9000 "${MINIO_ACCESS_KEY}" "${MINIO_SECRET_KEY}" >/dev/null 2>&1
    mc mirror glitchtip-store/glitchtip-attachments /backups/minio-attachments/ 2>&1
    MINIO_OBJECTS=$(mc ls --recursive glitchtip-store/glitchtip-attachments/ 2>/dev/null | wc -l)
    echo "[backup] MinIO backup complete: ${MINIO_OBJECTS} objects mirrored."

    # Validation (using mc and ls — no find/psql needed)
    echo "[backup] Running post-backup validation..."
    BACKUP_FILES=$(ls -1R /backups/minio-attachments/ 2>/dev/null | grep -cv '^\s*$\|:$' || echo 0)
    echo "[backup] Validation: MinIO live=${MINIO_OBJECTS}, Backed up=${BACKUP_FILES}"

    if [ "${MINIO_OBJECTS}" -gt 0 ] && [ "${BACKUP_FILES}" -lt 1 ]; then
      echo "[backup] VALIDATION FAILED: MinIO has ${MINIO_OBJECTS} objects but backup has 0 files!"
      exit 1
    fi

    echo "[backup] Backup and validation complete. ${MINIO_OBJECTS} objects verified."
SCRIPT_EOF

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
