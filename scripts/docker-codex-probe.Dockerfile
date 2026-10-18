FROM alpine:3.22.2@sha256:4b7ce07002c69e8f3d704a9c5d6fd3053be500b7f1c69fc0d80990c2ad8dd412
RUN addgroup -g 10001 agent && adduser -D -u 10001 -G agent agent \
    && mkdir /work /codex && chown agent:agent /work /codex
COPY --chmod=0555 codex /usr/local/bin/codex
ENV HOME=/home/agent CODEX_HOME=/codex
USER agent
WORKDIR /work
CMD ["sleep", "86400"]
