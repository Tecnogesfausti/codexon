FROM ghcr.io/home-assistant/amd64-base-debian:bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.1.7
ARG CODEX_NPM_PACKAGE="@openai/codex"
ARG ESPHOME_PIP_PACKAGE="esphome"

LABEL \
  io.hass.name="Codex Agent" \
  io.hass.description="Codex CLI agent with broad Home Assistant Supervisor access" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  NODE_ENV=production \
  CODEX_HOME=/data/codex

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    bubblewrap \
    ca-certificates \
    curl \
    git \
    jq \
    less \
    nano \
    nodejs \
    npm \
    openssh-server \
    python3 \
    python3-pip \
    python3-venv \
    ripgrep \
    rsync \
    shellcheck \
    sudo \
    tmux \
    unzip \
    util-linux \
    vim-tiny \
    yq \
  && npm install -g "${CODEX_NPM_PACKAGE}" \
  && npm install -g @modelcontextprotocol/server-filesystem @modelcontextprotocol/server-memory \
  && python3 -m venv /opt/esphome \
  && /opt/esphome/bin/pip install --no-cache-dir --upgrade pip wheel \
  && /opt/esphome/bin/pip install --no-cache-dir "${ESPHOME_PIP_PACKAGE}" \
  && ln -sf /opt/esphome/bin/esphome /usr/local/bin/esphome \
  && case "${BUILD_ARCH}" in \
      amd64) TTYD_ARCH="x86_64" ;; \
      aarch64) TTYD_ARCH="aarch64" ;; \
      *) echo "Unsupported ttyd architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac \
  && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${TTYD_ARCH}" -o /usr/local/bin/ttyd \
  && chmod +x /usr/local/bin/ttyd \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY rootfs/ /
RUN chmod +x \
  /usr/local/bin/codex-agent-entrypoint \
  /usr/local/bin/codex-agent-shell \
  /usr/local/bin/codex-login-chatgpt \
  /usr/local/bin/esphome-version-check \
  /usr/local/bin/ha-api \
  /usr/local/bin/ha-call-service \
  /usr/local/bin/ha-services \
  /usr/local/bin/ha-states \
  /usr/local/bin/host-shell \
  /usr/local/bin/web-terminal.sh \
  /usr/local/bin/supervisor-api

CMD ["/usr/local/bin/codex-agent-entrypoint"]
