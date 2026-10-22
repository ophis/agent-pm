ARG USER_IMAGE
FROM ${USER_IMAGE}
USER root
ARG AGENT_UID
ARG AGENT_GID
RUN test "$(id -u agent):$(id -g agent)" = "${AGENT_UID}:${AGENT_GID}"
ARG GH_VERSION=2.102.0
# ripgrep: OpenCode downloads an unchecked rg when none is on PATH.
# /usr/local/bin precedes /usr/bin on PATH: the gh wrapper runs this gh, not a gh the user image installs.
RUN apt-get update && apt-get install -y --no-install-recommends tini python3 python3-venv curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(dpkg --print-architecture)" in \
         amd64) arch=amd64 sum=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386 ;; \
         arm64) arch=arm64 sum=7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484 ;; \
         *) exit 1 ;; \
       esac \
    && name="gh_${GH_VERSION}_linux_${arch}" \
    && curl -fsSL -o "/tmp/${name}.tar.gz" "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${name}.tar.gz" \
    && echo "${sum}  /tmp/${name}.tar.gz" | sha256sum -c - \
    && tar -xzf "/tmp/${name}.tar.gz" -C /usr/local/bin --strip-components=2 "${name}/bin/gh" \
    && rm "/tmp/${name}.tar.gz" \
    && chown root:root /usr/local/bin/gh && chmod 755 /usr/local/bin/gh
COPY session/src /opt/agent-pm/session/src
COPY utils/src /opt/agent-pm/utils/src
COPY delegate/src /opt/agent-pm/delegate/src
COPY plugin-sources.json /opt/agent-pm/plugin-sources.json
COPY .claude-plugin/plugin.json /opt/agent-pm/version.json
COPY bootstrap.py codex-update.py opencode-install.py plugin-install.py hook-stub with-secrets workspace-check gh git-credential-agent-pm /opt/agent-pm/lib/
COPY claude.json /opt/agent-pm/templates/.claude.json
RUN npm install --global --ignore-scripts --prefix /opt/agent-pm/clients @openai/codex@0.163.0 \
    && python3 -I /opt/agent-pm/lib/opencode-install.py "$(dpkg --print-architecture)" /opt/agent-pm/clients \
    && PATH=/opt/agent-pm/clients/bin:/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin python3 -I /opt/agent-pm/lib/plugin-install.py /opt/agent-pm/plugin-sources.json /opt/agent-pm \
    && mkdir -p /opt/agent-pm/bin /opt/agent-pm/runtime
COPY codex /opt/agent-pm/bin/codex
# python3 on the agent's PATH is this venv's, importing agent-pm; system site keeps pip --user working.
RUN python3 -m venv --system-site-packages /opt/agent-pm/venv && mkdir -p /opt/agent-pm/bin \
    && printf '%s\n' /opt/agent-pm/delegate/src /opt/agent-pm/session/src /opt/agent-pm/utils/src > "$(/opt/agent-pm/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/agent-pm.pth" \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/agent-pm/venv/bin/python "$@"' > /opt/agent-pm/bin/python3 \
    && ln -s /opt/agent-pm/delegate/src/agent_pm/delegate/team /opt/agent-pm/team \
    && printf '%s\n' '#!/bin/sh' 'export PYTHONPATH=/opt/agent-pm/session/src:/opt/agent-pm/utils/src' 'exec /opt/agent-pm/venv/bin/python -P -m agent_pm.session.cli "$@"' > /opt/agent-pm/bin/agent-pm-session \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/agent-pm/venv/bin/python -I /opt/agent-pm/session/src/agent_pm/session/hosts/docker/relay.py "$@"' > /opt/agent-pm/bin/relay \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/agent-pm/venv/bin/python -I /opt/agent-pm/session/src/agent_pm/session/hosts/docker/call.py call "$@"' > /opt/agent-pm/bin/agent-pm-host-call \
    && ln -s /opt/agent-pm/lib/with-secrets /opt/agent-pm/bin/with-secrets \
    && ln -s /opt/agent-pm/clients/bin/opencode /opt/agent-pm/bin/opencode \
    && ln -s /opt/agent-pm/lib/gh /opt/agent-pm/bin/gh \
    && ln -s /opt/agent-pm/lib/git-credential-agent-pm /opt/agent-pm/bin/git-credential-agent-pm \
    && printf '%s\n' '[credential]' 'helper = /opt/agent-pm/bin/git-credential-agent-pm' 'useHttpPath = true' >> /etc/gitconfig \
    && printf '%s\n' '[core]' 'hooksPath = .githooks' >> /etc/gitconfig \
    && printf '%s\n' 'PATH=/opt/agent-pm/bin:$PATH' > /etc/profile.d/agent-pm.sh \
    && chmod 755 /opt/agent-pm/bin/codex /opt/agent-pm/bin/agent-pm-session /opt/agent-pm/bin/agent-pm-host-call /opt/agent-pm/bin/relay /opt/agent-pm/bin/python3 /opt/agent-pm/lib/with-secrets /opt/agent-pm/lib/hook-stub /opt/agent-pm/lib/workspace-check /opt/agent-pm/lib/gh /opt/agent-pm/lib/git-credential-agent-pm \
    && chmod -R a+rX /opt/agent-pm/codex-plugins \
    && chmod -R a+rX /opt/agent-pm/claude-plugins \
    && chmod -R a+rX /opt/agent-pm/plugins \
    && chown -R root:root /opt/agent-pm && chmod -R go-w /opt/agent-pm \
    && install -d -o agent -g agent -m 0700 /run/agent-pm /run/agent-pm/relay-queue
ARG AGENT_PM_VERSION
LABEL agent-pm.version=${AGENT_PM_VERSION} agent-pm.agent-uid=${AGENT_UID} agent-pm.agent-gid=${AGENT_GID}
ENV PATH=/opt/agent-pm/bin:/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
# Claude Code loads each plugin here as <name>@inline, keeping its /<name>: commands.
ARG AGENT_PM_PLUGIN_DIRS
ENV CLAUDE_CODE_PLUGIN_DIRS=${AGENT_PM_PLUGIN_DIRS}
USER agent
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
