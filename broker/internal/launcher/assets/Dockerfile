ARG USER_IMAGE
FROM ${USER_IMAGE}
USER root
ARG AGENT_UID
ARG AGENT_GID
RUN test "$(id -u agent):$(id -g agent)" = "${AGENT_UID}:${AGENT_GID}"
RUN apt-get update && apt-get install -y --no-install-recommends tini python3 python3-venv && rm -rf /var/lib/apt/lists/*
COPY session/src /opt/agent-pm/session/src
COPY utils/src /opt/agent-pm/utils/src
COPY delegate/src /opt/agent-pm/delegate/src
COPY .claude-plugin/plugin.json /opt/agent-pm/version.json
COPY bootstrap.py hook-stub with-secrets workspace-check gh git-credential-agent-pm /opt/agent-pm/lib/
COPY claude.json /opt/agent-pm/templates/.claude.json
# python3 on the agent's PATH is this venv's, importing agent-pm; system site keeps pip --user working.
RUN python3 -m venv --system-site-packages /opt/agent-pm/venv && mkdir -p /opt/agent-pm/bin \
    && printf '%s\n' /opt/agent-pm/delegate/src /opt/agent-pm/session/src /opt/agent-pm/utils/src > "$(/opt/agent-pm/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/agent-pm.pth" \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/agent-pm/venv/bin/python "$@"' > /opt/agent-pm/bin/python3 \
    && ln -s /opt/agent-pm/delegate/src/agent_pm/delegate/team /opt/agent-pm/team \
    && printf '%s\n' '#!/bin/sh' 'export PYTHONPATH=/opt/agent-pm/session/src:/opt/agent-pm/utils/src' 'exec /opt/agent-pm/venv/bin/python -P -m agent_pm.session.cli "$@"' > /opt/agent-pm/bin/agent-pm-session \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/agent-pm/venv/bin/python -I /opt/agent-pm/session/src/agent_pm/session/hosts/docker/relay.py "$@"' > /opt/agent-pm/bin/relay \
    && ln -s /opt/agent-pm/lib/with-secrets /opt/agent-pm/bin/with-secrets \
    && ln -s /opt/agent-pm/lib/gh /opt/agent-pm/bin/gh \
    && ln -s /opt/agent-pm/lib/git-credential-agent-pm /opt/agent-pm/bin/git-credential-agent-pm \
    && printf '%s\n' '[credential]' 'helper = /opt/agent-pm/bin/git-credential-agent-pm' 'useHttpPath = true' >> /etc/gitconfig \
    && chmod 755 /opt/agent-pm/bin/agent-pm-session /opt/agent-pm/bin/relay /opt/agent-pm/bin/python3 /opt/agent-pm/lib/with-secrets /opt/agent-pm/lib/hook-stub /opt/agent-pm/lib/workspace-check /opt/agent-pm/lib/gh /opt/agent-pm/lib/git-credential-agent-pm \
    && chown -R root:root /opt/agent-pm && chmod -R go-w /opt/agent-pm \
    && install -d -o agent -g agent -m 0700 /run/agent-pm /run/agent-pm/relay-queue
ARG AGENT_PM_VERSION
LABEL agent-pm.version=${AGENT_PM_VERSION} agent-pm.agent-uid=${AGENT_UID} agent-pm.agent-gid=${AGENT_GID}
ENV PATH=/opt/agent-pm/bin:/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
USER agent
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
