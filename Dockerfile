FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.1.0

ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024

ENV SKIP_BLEATER_BOOT=1
ENV ALLOWED_NAMESPACES="glitchtip,keycloak"

# MinIO server image (minio/minio:latest) is pre-cached in base image from bleater.
# Pull mc (MinIO client) for backup tooling.
RUN mkdir -p /var/lib/rancher/k3s/agent/images && \
    apt-get update -qq && \
    apt-get install -y -qq skopeo && \
    skopeo copy --override-os linux --override-arch amd64 docker://docker.io/minio/mc:RELEASE.2024-11-21T17-21-54Z docker-archive:/var/lib/rancher/k3s/agent/images/mc.tar:docker.io/minio/mc:RELEASE.2024-11-21T17-21-54Z && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
