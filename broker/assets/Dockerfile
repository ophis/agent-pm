ARG USER_IMAGE
FROM ${USER_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends tini python3 python3-venv && rm -rf /var/lib/apt/lists/*
COPY session/src /opt/agent-pm/session/src
COPY utils/src /opt/agent-pm/utils/src
COPY .claude-plugin/plugin.json /opt/agent-pm/version.json
COPY bootstrap.py hook-stub with-secrets /opt/agent-pm/lib/
COPY claude.json /opt/agent-pm/templates/.claude.json
RUN python3 -m venv /opt/agent-pm/venv && mkdir -p /opt/agent-pm/bin \
    && printf '%s\n' '#!/bin/sh' 'export PYTHONPATH=/opt/agent-pm/session/src:/opt/agent-pm/utils/src' 'exec /opt/agent-pm/venv/bin/python -P -m agent_pm.session.cli "$@"' > /opt/agent-pm/bin/agent-pm-session \
    && ln -s /opt/agent-pm/lib/with-secrets /opt/agent-pm/bin/with-secrets \
    && chmod 755 /opt/agent-pm/bin/agent-pm-session /opt/agent-pm/lib/with-secrets /opt/agent-pm/lib/hook-stub \
    && cp -a /home/agent/.npm-global /opt/agent-pm/client-baseline \
    && chown -R root:root /opt/agent-pm && chmod -R go-w /opt/agent-pm
ARG AGENT_PM_VERSION
LABEL agent-pm.version=${AGENT_PM_VERSION}
ENV PATH=/opt/agent-pm/bin:/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
USER agent
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
