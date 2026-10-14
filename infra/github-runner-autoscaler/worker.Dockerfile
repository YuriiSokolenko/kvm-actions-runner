ARG RUNNER_PLATFORM=linux/amd64
FROM --platform=${RUNNER_PLATFORM} python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG NODE_VERSION=26.11.1
ARG NODE_SHA256=3883bfc73f9a680ca4eab04b196068aaaab1373ffa77d8fc1a4408222495b651
ARG NPM_VERSION=12.2.0
ARG ACTIONS_RUNNER_VERSION=2.338.0
ARG ACTIONS_RUNNER_SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
ARG PI_CODING_AGENT_VERSION=1.1.0
ARG PI_MCP_ADAPTER_VERSION=5.2.0
ARG MCP_SEARXNG_VERSION=2.5.1
ARG PI_SUBAGENTS_VERSION=0.76.1
ARG ORBIT_VERSION=0.139.0
ARG ORBIT_SHA256=c1880eda51c9bb7596e9790d44555fed5ebc65b34012c890a99565a69a2d723c
ARG ORBIT_DUCKDB_VERSION=1.5.5
ARG DUCKDB_JSON_SHA256=325c0e08e081a928c66bba1528f3848e54dade9f82a8afe84f97df137333962e
ARG LSP_MCP_SERVER_VERSION=1.1.26
ARG GIT_CONTEXT_MCP_VERSION=1.0.0
ARG AST_GREP_VERSION=0.50.0
ARG BASEDPYRIGHT_VERSION=1.40.2
ARG KOTLIN_LSP_VERSION=263.6379.0
ARG KOTLIN_LSP_SHA256=ab8ca4455dc2fc5fe1a24db2bccc46c104254d2c465155c4251ee65df8f3f7cc
ARG MINI_SWE_AGENT_VERSION=2.4.6

USER root
COPY infra/github-runner-autoscaler/worker-entrypoint.sh /usr/local/bin/runner-entrypoint
COPY infra/github-runner-autoscaler/lsp-mcp-server-wrapper.mjs /tmp/lsp-mcp-server-wrapper.mjs
# The build context can contain mode 0600 files; allow the unprivileged runner
# to read its startup preflight script regardless of source checkout modes.
COPY --chmod=0755 infra/github-runner-autoscaler/check-pi-searxng-mcp.mjs /usr/local/bin/check-pi-searxng-mcp
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq python3 python3-venv sqlite3 sudo tar gzip xz-utils \
      libatomic1 libcurl4 libgcc-s1 libicu76 libkrb5-3 liblttng-ust1 libssl3t64 libstdc++6 libunwind8 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /usr/local \
    && rm /tmp/node.tar.xz \
    && npm install --global "npm@${NPM_VERSION}" \
    && useradd --create-home --uid 1001 --shell /bin/bash runner \
    && printf 'runner ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner \
    && install -d -o runner -g runner /home/runner/actions-runner /home/runner/build-tools /opt/kotlin-lsp \
    && curl -fsSL \
      "https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/actions-runner-linux-x64-${ACTIONS_RUNNER_VERSION}.tar.gz" \
      -o /tmp/actions-runner.tar.gz \
    && echo "${ACTIONS_RUNNER_SHA256}  /tmp/actions-runner.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/actions-runner.tar.gz -C /home/runner/actions-runner \
    && rm /tmp/actions-runner.tar.gz \
    && curl -fsSL \
      "https://gitlab.com/api/v4/projects/77960826/packages/generic/orbit-cli/${ORBIT_VERSION}/orbit-cli-linux-x86_64.tar.gz" \
      -o /tmp/orbit-cli.tar.gz \
    && echo "${ORBIT_SHA256}  /tmp/orbit-cli.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/orbit-cli.tar.gz -C /usr/local/bin orbit \
    && chmod 0755 /usr/local/bin/orbit \
    && rm /tmp/orbit-cli.tar.gz \
    && install -d -o runner -g runner "/home/runner/.duckdb/extensions/v${ORBIT_DUCKDB_VERSION}/linux_amd64" \
    && curl -fsSL \
      "https://extensions.duckdb.org/v${ORBIT_DUCKDB_VERSION}/linux_amd64/json.duckdb_extension.gz" \
      -o /tmp/json.duckdb_extension.gz \
    && echo "${DUCKDB_JSON_SHA256}  /tmp/json.duckdb_extension.gz" | sha256sum -c - \
    && gzip -dc /tmp/json.duckdb_extension.gz \
      > "/home/runner/.duckdb/extensions/v${ORBIT_DUCKDB_VERSION}/linux_amd64/json.duckdb_extension" \
    && chown runner:runner "/home/runner/.duckdb/extensions/v${ORBIT_DUCKDB_VERSION}/linux_amd64/json.duckdb_extension" \
    && rm /tmp/json.duckdb_extension.gz \
    && chown -R runner:runner /home/runner/actions-runner /opt/kotlin-lsp \
    && npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_CODING_AGENT_VERSION}" \
      "lsp-mcp-server@${LSP_MCP_SERVER_VERSION}" "git-context-mcp@${GIT_CONTEXT_MCP_VERSION}" \
      "mcp-searxng@${MCP_SEARXNG_VERSION}" \
    && npm install --prefix /opt/ast-grep "@ast-grep/cli@${AST_GREP_VERSION}" \
    && ln -s /opt/ast-grep/node_modules/.bin/ast-grep /usr/local/bin/ast-grep \
    && python3 -m venv /opt/basedpyright \
    && /opt/basedpyright/bin/python -m pip install --no-cache-dir "basedpyright==${BASEDPYRIGHT_VERSION}" \
    && nodejs_wheel_bin="$(/opt/basedpyright/bin/python -c 'from nodejs_wheel.executable import ROOT_DIR; import os; print(os.path.join(ROOT_DIR, "bin", "node"))')" \
    && test -x "$nodejs_wheel_bin" \
    && ln -sf /usr/local/bin/node "$nodejs_wheel_bin" \
    && ln -s /opt/basedpyright/bin/basedpyright /usr/local/bin/basedpyright \
    && ln -s /opt/basedpyright/bin/basedpyright-langserver /usr/local/bin/basedpyright-langserver \
    && python3 -m venv /opt/mini-swe-agent \
    && /opt/mini-swe-agent/bin/python -m pip install --no-cache-dir "mini-swe-agent==${MINI_SWE_AGENT_VERSION}" \
    && ln -s /opt/mini-swe-agent/bin/mini /usr/local/bin/mini \
    && curl -fsSL "https://download.jetbrains.com/language-server/kotlin-server/${KOTLIN_LSP_VERSION}/kotlin-server-${KOTLIN_LSP_VERSION}.tar.gz" -o /tmp/kotlin-lsp.tar.gz \
    && echo "${KOTLIN_LSP_SHA256}  /tmp/kotlin-lsp.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/kotlin-lsp.tar.gz --strip-components=1 -C /opt/kotlin-lsp \
    && rm /tmp/kotlin-lsp.tar.gz \
    && ln -s /opt/kotlin-lsp/bin/intellij-server /usr/local/bin/kotlin-lsp \
    && install -d -o runner -g runner /opt/pi-package-seed \
    && chmod 0755 /usr/local/bin/runner-entrypoint \
    && rm -f /usr/local/bin/lsp-mcp-server \
    && install -m 0755 /tmp/lsp-mcp-server-wrapper.mjs /usr/local/bin/lsp-mcp-server \
    && rm /tmp/lsp-mcp-server-wrapper.mjs \
    && pi --version \
    && test -x "$(command -v mcp-searxng)" \
    && node --version \
    && npm --version \
    && ast-grep --version \
    && orbit version \
    && mini --help >/dev/null

COPY --chown=1001:1001 infra/github-runner-autoscaler/patch-pi-mcp-adapter.mjs /home/runner/build-tools/patch-pi-mcp-adapter.mjs
# Mirror the repository layout so the patch can import the shared source.
# The source patch and shared resume helper are copied before the install RUN.
# Docker invalidates the patched /opt/pi-package-seed layer when either changes.
# Entrypoint restores this image seed; never double-patch it at job startup.
COPY --chown=1001:1001 infra/github-runner-autoscaler/patch-pi-subagents-planner-terminal.mjs /home/runner/build-tools/infra/github-runner-autoscaler/patch-pi-subagents-planner-terminal.mjs
COPY --chown=1001:1001 scripts/pi-common/restored-work.mjs /home/runner/build-tools/scripts/pi-common/restored-work.mjs

USER runner
ENV HOME=/home/runner \
    npm_config_cache=/tmp/pi-runner-npm-cache \
    PI_MCP_ADAPTER_VERSION=${PI_MCP_ADAPTER_VERSION} \
    PI_SUBAGENTS_VERSION=${PI_SUBAGENTS_VERSION}
RUN pi install --no-approve "npm:pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}" \
    && pi install --no-approve "npm:pi-subagents@${PI_SUBAGENTS_VERSION}" \
    && node /home/runner/build-tools/patch-pi-mcp-adapter.mjs /home/runner/.pi/agent/npm/node_modules/pi-mcp-adapter/package.json \
    && node /home/runner/build-tools/infra/github-runner-autoscaler/patch-pi-subagents-planner-terminal.mjs /home/runner/.pi/agent/npm/node_modules/pi-subagents \
    && node --check /home/runner/.pi/agent/npm/node_modules/pi-subagents/src/runs/foreground/execution.js \
    && mkdir -p /opt/pi-package-seed \
    && cp -a /home/runner/.pi/agent/npm /opt/pi-package-seed/
USER root
RUN chmod -R a+rX /opt/pi-package-seed \
    && rm /home/runner/build-tools/patch-pi-mcp-adapter.mjs /home/runner/build-tools/infra/github-runner-autoscaler/patch-pi-subagents-planner-terminal.mjs /home/runner/build-tools/scripts/pi-common/restored-work.mjs

USER runner
WORKDIR /home/runner/actions-runner
ENTRYPOINT ["/usr/local/bin/runner-entrypoint"]
