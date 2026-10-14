FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl jq netcat-openbsd qemu-system-x86 \
      qemu-utils util-linux xorriso \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/kvm
COPY admission.py host_probe.py github_api.py manager.py /opt/kvm/
COPY kvm-backend.py kvm-egress-proxy.py kvm-proxy-bridge /usr/local/bin/
RUN chmod 0755 /usr/local/bin/kvm-backend.py /usr/local/bin/kvm-proxy-bridge /opt/kvm/manager.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/opt/kvm/manager.py"]
