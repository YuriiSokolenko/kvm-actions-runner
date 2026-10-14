FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS node-runtime

ARG NODE_VERSION=26.11.1
ARG NODE_SHA256=3883bfc73f9a680ca4eab04b196068aaaab1373ffa77d8fc1a4408222495b651
ARG NPM_VERSION=12.2.0
ARG TYPEBOX_VERSION=1.3.27
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libatomic1 libgcc-s1 libstdc++6 tar xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /usr/local \
    && rm /tmp/node.tar.xz \
    && npm install --global --no-audit --no-fund "npm@${NPM_VERSION}" "typebox@${TYPEBOX_VERSION}" \
    && node --version && npm --version

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ARG RUFF_VERSION=0.17.0
ARG PYTEST_VERSION=9.1.1
ARG PYTEST_ASYNCIO_VERSION=1.4.0
ARG PYYAML_VERSION=6.0.3
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

COPY infra/github-runner-autoscaler/run-check-sandbox-exec.py /usr/local/lib/run-check-sandbox-exec.py
COPY infra/github-runner-autoscaler/run-check-sandbox-probe.py /usr/local/lib/run-check-sandbox-probe.py

RUN apt-get update \
    # libatomic1: the copied official Node binary links against libatomic.so.1 (#635 probe).
    && apt-get install -y --no-install-recommends ca-certificates git libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir \
      "ruff==${RUFF_VERSION}" \
      "pytest==${PYTEST_VERSION}" \
      "pytest-asyncio==${PYTEST_ASYNCIO_VERSION}" \
      "PyYAML==${PYYAML_VERSION}" \
      'mcp[cli]==2.3.0' \
      'fastapi==0.143.0' \
      'uvicorn[standard]==0.54.0' \
      'httpx==0.28.1' \
      'pydantic==2.14.0' \
      'pydantic-settings==2.15.0' \
      'cryptography==50.0.2' \
      'itsdangerous==2.2.0' \
      'python-multipart==0.0.32' \
    && mkdir -p /node_modules /workspace \
    && ln -s /usr/local/lib/node_modules/typebox /node_modules/typebox \
    && chown 1001:1001 /workspace \
    && chmod 0755 /workspace \
    && chmod 0555 /usr/local/lib/run-check-sandbox-exec.py /usr/local/lib/run-check-sandbox-probe.py

ENV PATH=/usr/local/bin:/usr/bin:/bin \
    HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/workspace/src \
    RUFF_CACHE_DIR=/tmp/ruff

WORKDIR /workspace
USER 1001:1001
