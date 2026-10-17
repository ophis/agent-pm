package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"os/user"
	"regexp"
	"strings"
	"syscall"
	"unicode/utf8"

	"github.com/ophis/agent-pm/broker/internal/broker"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/launcher"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const usage = "usage: agent-pm-broker version | login claude | build --agent-pm DIR | start --manifest FILE | close --container NAME"

var containerPattern = regexp.MustCompile(`^agent-pm-[A-Za-z0-9_-]+-[0-9a-f]{32}$`)

type step func(ctx context.Context, arg string) (any, error)

type app struct {
	build, start, close step
	login               func(out io.Writer) error
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr, system())
	cancel()
	os.Exit(code)
}

func accountHome() (string, error) {
	u, err := user.Current()
	if err != nil || !strings.HasPrefix(u.HomeDir, "/") {
		return "", errors.Join(errors.New("cannot resolve system account home"), err)
	}
	return u.HomeDir, nil
}

func isPath(s string) bool {
	return strings.HasPrefix(s, "/") && utf8.ValidString(s) && !strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer, a app) (code int) {
	command := "unknown"
	if len(args) > 0 {
		switch args[0] {
		case "version", "build", "start", "close":
			command = args[0]
		case "login":
			if len(args) == 2 && args[1] == "claude" {
				if err := a.login(stdout); err != nil {
					fmt.Fprintln(stderr, "agent-pm-broker:", err)
					return 1
				}
				return 0
			}
		}
	}
	defer func() {
		if r := recover(); r != nil {
			e := protocol.New(protocol.InternalError, "Unexpected broker failure.")
			e.Cause = fmt.Sprint(r)
			code = fail(stderr, command, e)
		}
	}()
	var s step
	var arg string
	switch {
	case command == "version" && len(args) == 1:
		return emit(stdout, map[string]string{"protocol_version": protocol.Version})
	case command == "build" && len(args) == 3 && args[1] == "--agent-pm" && isPath(args[2]):
		s, arg = a.build, args[2]
	case command == "start" && len(args) == 3 && args[1] == "--manifest" && isPath(args[2]):
		s, arg = a.start, args[2]
	case command == "close" && len(args) == 3 && args[1] == "--container" && containerPattern.MatchString(args[2]):
		s, arg = a.close, args[2]
	default:
		return fail(stderr, command, protocol.New(protocol.Usage, usage))
	}
	result, err := s(ctx, arg)
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.InternalError, "Unexpected broker failure.")
			e.Cause = err.Error()
		}
		return fail(stderr, command, e)
	}
	return emit(stdout, result)
}

func emit(stdout io.Writer, v any) int {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	if _, err := stdout.Write(append(data, '\n')); err != nil {
		return protocol.Exit(protocol.InternalError)
	}
	return 0
}

func fail(stderr io.Writer, command string, e *protocol.Error) int {
	_, _ = stderr.Write(protocol.Envelope(command, e))
	return protocol.Exit(e.Code)
}

// host wires the launcher to the fixed Docker Desktop CLI; errors are protocol errors.
func host() (launcher.Host, func(), error) {
	home, err := accountHome()
	if err != nil {
		return launcher.Host{}, nil, err
	}
	d, clean, err := docker.New(home)
	if err != nil {
		return launcher.Host{}, nil, protocol.New(protocol.DockerUnreachable, err.Error())
	}
	return launcher.Host{Home: home, Docker: d}, clean, nil
}

func system() app {
	return app{
		build: func(ctx context.Context, pkg string) (any, error) {
			h, clean, err := host()
			if err != nil {
				return nil, err
			}
			defer clean()
			return h.Build(ctx, pkg)
		},
		login: func(out io.Writer) error {
			home, err := accountHome()
			if err != nil {
				return err
			}
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			return broker.Login(func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Claude setup-token (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}, broker.SystemKeychain{Home: home}, out)
		},
	}
}
