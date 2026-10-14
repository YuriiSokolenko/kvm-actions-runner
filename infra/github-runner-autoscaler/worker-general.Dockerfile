ARG RUNNER_PLATFORM=linux/amd64
FROM --platform=${RUNNER_PLATFORM} python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS python-runtime
FROM --platform=${RUNNER_PLATFORM} debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG NODE_VERSION=26.11.1
ARG NODE_SHA256=3883bfc73f9a680ca4eab04b196068aaaab1373ffa77d8fc1a4408222495b651
ARG NPM_VERSION=12.2.0
ARG ACTIONS_RUNNER_VERSION=2.338.0
ARG ACTIONS_RUNNER_SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
ARG DOCKER_BUILDX_VERSION=0.38.0-1~debian.12~bookworm
ARG DOCKER_CLI_VERSION=5:29.9.0-1~debian.12~bookworm
ARG DOCKER_COMPOSE_VERSION=5.6.0-1~debian.12~bookworm
ARG GH_CLI_VERSION=2.102.0
ARG GH_CLI_KEYRING_SHA256=6084d5d7bd8e288441e0e94fc6275570895da18e6751f70f057485dc2d1a811b
ENV HOME=/home/runner \
    ACTIONS_RUNNER_VERSION=${ACTIONS_RUNNER_VERSION}

USER root
COPY --from=python-runtime /usr/local/ /usr/local/
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git gnupg jq sudo tar gzip xz-utils \
      libatomic1 libcurl4 libgcc-s1 libicu72 libkrb5-3 liblttng-ust1 libssl3 libstdc++6 libunwind8 zlib1g \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /usr/local \
    && rm /tmp/node.tar.xz \
    && HOME=/root npm_config_cache=/tmp/npm-cache npm install --global "npm@${NPM_VERSION}" \
    && rm -rf /tmp/npm-cache \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "${GH_CLI_KEYRING_SHA256}  /etc/apt/keyrings/githubcli-archive-keyring.gpg" | sha256sum -c - \
    && chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      "gh=${GH_CLI_VERSION}" "docker-ce-cli=${DOCKER_CLI_VERSION}" \
      "docker-compose-plugin=${DOCKER_COMPOSE_VERSION}" "docker-buildx-plugin=${DOCKER_BUILDX_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version \
    && useradd --create-home --uid 1001 --shell /bin/bash runner \
    && groupadd --gid 983 hostdocker \
    && usermod --append --groups hostdocker runner \
    && printf 'runner ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner \
    && install -d -o runner -g runner /home/runner/actions-runner

RUN curl -fsSL \
      "https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/actions-runner-linux-x64-${ACTIONS_RUNNER_VERSION}.tar.gz" \
      -o /tmp/actions-runner.tar.gz \
    && echo "${ACTIONS_RUNNER_SHA256}  /tmp/actions-runner.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/actions-runner.tar.gz -C /home/runner/actions-runner \
    && rm /tmp/actions-runner.tar.gz \
    && chown -R runner:runner /home/runner/actions-runner

COPY infra/github-runner-autoscaler/worker-entrypoint.sh /usr/local/bin/runner-entrypoint
RUN chmod 0755 /usr/local/bin/runner-entrypoint

WORKDIR /home/runner/actions-runner
USER runner
ENTRYPOINT ["/usr/local/bin/runner-entrypoint"]
