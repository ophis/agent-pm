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
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/ophis/agent-pm/broker/internal/broker"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/launcher"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const usage = "usage: agent-pm-broker version | login claude | login github agent-pm-engineer|agent-pm-docs | build --agent-pm DIR [--max-age SECONDS] | start --manifest FILE | close --container NAME"

var containerPattern = regexp.MustCompile(`^agent-pm-[A-Za-z0-9_-]+-[0-9a-f]{32}$`)
var secondsPattern = regexp.MustCompile(`^[0-9]{1,9}$`)

// step runs a command; progress is the broker's stderr, for build's Docker progress and start's phase times.
type step func(ctx context.Context, arg string, progress io.Writer) (any, error)

type app struct {
	build        func(ctx context.Context, pkg string, maxAge time.Duration, progress io.Writer) (any, error)
	start, close step
	login        func(out io.Writer) error
	loginGitHub  func(app string, out io.Writer) error
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
			var login func() error
			switch {
			case len(args) == 2 && args[1] == "claude":
				login = func() error { return a.login(stdout) }
			case len(args) == 3 && args[1] == "github" && broker.GitHubApps[args[2]]:
				login = func() error { return a.loginGitHub(args[2], stdout) }
			}
			if login != nil {
				if err := login(); err != nil {
					fmt.Fprintln(stderr, "agent-pm-broker:", err)
					return 1
				}
				return 0
			}
		}
	}
	defer func() {
		if r := recover(); r != nil {
			code = fail(stderr, command, protocol.New(protocol.InternalError, "Unexpected broker failure."))
		}
	}()
	var s step
	var arg string
	switch {
	case command == "version" && len(args) == 1:
		return emit(stdout, map[string]string{"protocol_version": protocol.Version})
	case command == "build" && (len(args) == 3 || (len(args) == 5 && args[3] == "--max-age" && secondsPattern.MatchString(args[4]))) &&
		args[1] == "--agent-pm" && isPath(args[2]):
		maxAge := time.Duration(-1)
		if len(args) == 5 {
			n, _ := strconv.Atoi(args[4])
			maxAge = time.Duration(n) * time.Second
		}
		s, arg = func(ctx context.Context, pkg string, progress io.Writer) (any, error) {
			return a.build(ctx, pkg, maxAge, progress)
		}, args[2]
	case command == "start" && len(args) == 3 && args[1] == "--manifest" && isPath(args[2]):
		s, arg = a.start, args[2]
	case command == "close" && len(args) == 3 && args[1] == "--container" && containerPattern.MatchString(args[2]):
		s, arg = a.close, args[2]
	default:
		return fail(stderr, command, protocol.New(protocol.Usage, usage))
	}
	result, err := s(ctx, arg, stderr)
	if err != nil {
		var e *protocol.Error
		if !errors.As(err, &e) {
			e = protocol.New(protocol.InternalError, "Unexpected broker failure.")
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

// withHost runs f on the launcher wired to the fixed Docker Desktop CLI; docker build's stderr and the launcher's notes go to progress.
func withHost(progress io.Writer, f func(launcher.Host) (any, error)) (any, error) {
	home, err := accountHome()
	if err != nil {
		return nil, err
	}
	d, clean, err := docker.New(home)
	if err != nil {
		return nil, err
	}
	defer clean()
	d.Progress = progress
	h := launcher.NewHost(home, d.Endpoint(), d, broker.Credentials{Keychain: broker.SystemKeychain{Home: home}})
	h.Progress = progress
	return f(h)
}

func system() app {
	return app{
		build: func(ctx context.Context, pkg string, maxAge time.Duration, progress io.Writer) (any, error) {
			return withHost(progress, func(h launcher.Host) (any, error) { return h.Build(ctx, pkg, maxAge) })
		},
		start: func(ctx context.Context, manifest string, progress io.Writer) (any, error) {
			return withHost(progress, func(h launcher.Host) (any, error) { return h.Start(ctx, manifest) })
		},
		close: func(ctx context.Context, container string, _ io.Writer) (any, error) {
			return withHost(nil, func(h launcher.Host) (any, error) { return h.Close(ctx, container) })
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
		loginGitHub: func(app string, out io.Writer) error {
			home, err := accountHome()
			if err != nil {
				return err
			}
			if err := broker.CheckInstalled(); err != nil {
				return err
			}
			return broker.LoginGitHub(app, broker.SystemKeychain{Home: home}, out)
		},
	}
}
