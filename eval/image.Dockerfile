# The evaluation's worker image: the project's toolchain image plus what every unit needs (git, the openspec CLI, the
# Claude Code CLI) and the `agent` user. A stand-in for the herd's role layer until Build plan step 3 builds it.
ARG TOOLCHAIN=localhost/driving-log-toolchain
FROM docker.io/library/node:22-bookworm-slim AS node
FROM ${TOOLCHAIN}
ARG OPENSPEC=1.13.2
ARG CLAUDE_CODE=2.1.296
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@fission-ai/openspec@${OPENSPEC}" "@anthropic-ai/claude-code@${CLAUDE_CODE}" \
 && npm cache clean --force \
 && useradd --uid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent
USER agent
WORKDIR /work
ENV HOME=/home/agent
