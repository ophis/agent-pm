package launcher

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const (
	updateTimeout = 200 * time.Second // in-container timeout (180 s + 10 s kill) fires first
	npmPrefix     = "/home/agent/.npm-global"
	restore       = "rm -rf " + npmPrefix + " && cp -a /opt/agent-pm/client-baseline " + npmPrefix
)

var networkError = regexp.MustCompile(`\b(ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENETUNREACH|EHOSTUNREACH)\b`)

var fallbackMessages = map[string]string{
	"network":        "npm could not reach its registry; using the image's clients.",
	"timeout":        "Client update exceeded 180 seconds; using the image's clients.",
	"command_failed": "Client update command failed; using the image's clients.",
	"version_failed": "An updated client failed --version; using the image's clients.",
}

func asAgent(container string, argv ...string) []string {
	return append([]string{"exec", "--user", "agent", container}, argv...)
}

func (h Host) probe(ctx context.Context, container string) (Clients, error) {
	var versions [2]string
	var errs []error
	for i, client := range []string{"claude", "codex"} {
		out, err := h.Docker.Run(ctx, asAgent(container, client, "--version"), nil)
		line, _, _ := strings.Cut(strings.TrimSpace(string(out)), "\n")
		versions[i] = strings.TrimSpace(line)
		if err == nil && (versions[i] == "" || hasControl(versions[i])) {
			err = errors.New(client + " --version gave no usable version")
		}
		errs = append(errs, err)
	}
	return Clients{Claude: versions[0], Codex: versions[1]}, errors.Join(errs...)
}

// updateClients updates both clients before any secret exists; any failure restores the image's clients.
func (h Host) updateClients(ctx context.Context, container string) (Clients, Update, error) {
	uctx, cancel := context.WithTimeout(ctx, updateTimeout)
	_, err := h.Docker.Run(uctx, asAgent(container, "timeout", "--kill-after=10", "180", "npm", "install", "-g", "--prefix", npmPrefix,
		"@anthropic-ai/claude-code@latest", "@openai/codex@latest"), nil)
	hung := uctx.Err() != nil
	cancel()
	var reason string
	var ce *docker.CommandError
	switch {
	case errors.Is(err, docker.ErrUnreachable):
		return Clients{}, Update{}, failure(err, protocol.StartFailed, "")
	case hung:
		return Clients{}, Update{}, protocol.New(protocol.StartFailed, "Client update did not return; Docker stopped responding.")
	case err == nil:
		clients, perr := h.probe(ctx, container)
		if errors.Is(perr, docker.ErrUnreachable) {
			return Clients{}, Update{}, failure(perr, protocol.StartFailed, "")
		}
		if perr == nil {
			return clients, Update{Status: "updated"}, nil
		}
		reason = "version_failed"
	case errors.As(err, &ce) && (ce.Exit == 124 || ce.Exit == 137):
		reason = "timeout"
	case errors.As(err, &ce) && networkError.MatchString(ce.Stderr):
		reason = "network"
	default:
		reason = "command_failed"
	}
	if _, err := h.Docker.Run(ctx, asAgent(container, "/bin/sh", "-c", restore), nil); err != nil {
		return Clients{}, Update{}, failure(err, protocol.StartFailed, "Restoring the image's clients failed.")
	}
	clients, err := h.probe(ctx, container)
	if err != nil {
		return Clients{}, Update{}, failure(err, protocol.StartFailed, "Cannot run the image's clients after restoring them.")
	}
	return clients, Update{Status: "fallback", Reason: reason, Message: fallbackMessages[reason]}, nil
}
