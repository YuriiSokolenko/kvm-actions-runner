FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG NODE_VERSION=26.11.1
ARG NODE_SHA256=3883bfc73f9a680ca4eab04b196068aaaab1373ffa77d8fc1a4408222495b651
ARG NPM_VERSION=12.2.0
ARG ACTIONS_RUNNER_VERSION=2.338.0
ARG ACTIONS_RUNNER_SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
ENV ACTIONS_RUNNER_BASELINE_VERSION=${ACTIONS_RUNNER_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gosu jq tar gzip xz-utils \
      libatomic1 libcurl4 libgcc-s1 libicu72 libkrb5-3 liblttng-ust1 libssl3 libstdc++6 libunwind8 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /usr/local \
    && rm /tmp/node.tar.xz \
    && npm install --global "npm@${NPM_VERSION}" \
    && useradd --create-home --uid 1001 --shell /bin/bash runner \
    && install -d -o runner -g runner \
      /opt/actions-runner-baseline \
      /home/runner/actions-runner

RUN curl -fsSL \
      "https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/actions-runner-linux-x64-${ACTIONS_RUNNER_VERSION}.tar.gz" \
      -o /tmp/actions-runner.tar.gz \
    && echo "${ACTIONS_RUNNER_SHA256}  /tmp/actions-runner.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner-baseline \
    && rm /tmp/actions-runner.tar.gz \
    && cp -a /opt/actions-runner-baseline/. /home/runner/actions-runner/ \
    && chown -R runner:runner /opt/actions-runner-baseline /home/runner/actions-runner

WORKDIR /home/runner/actions-runner

COPY infra/github-runner-autoscaler/control-runner-entrypoint.sh /usr/local/bin/control-runner-entrypoint
RUN chmod 0755 /usr/local/bin/control-runner-entrypoint

ENTRYPOINT ["/usr/local/bin/control-runner-entrypoint"]
