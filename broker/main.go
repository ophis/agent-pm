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
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/ophis/agent-pm/broker/internal/broker"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/github"
	"github.com/ophis/agent-pm/broker/internal/launcher"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const usage = "usage: agent-pm-broker version | login claude | login github agent-pm-bot | build --agent-pm DIR [--plugin NAME]... [--max-age SECONDS] | start --manifest FILE | refresh --container NAME --manifest FILE | close --container NAME | status | workspace-rm --volume NAME [--force]"

var containerPattern = regexp.MustCompile(`^agent-pm-[A-Za-z0-9_-]+-[0-9a-f]{32}$`)
var secondsPattern = regexp.MustCompile(`^[0-9]{1,9}$`)
var pluginPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?$`)
var volumePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$`)

// step runs a command; progress is the broker's stderr, for build's Docker progress and start's phase times.
type step func(ctx context.Context, arg string, progress io.Writer) (any, error)

type app struct {
	build        func(ctx context.Context, pkg string, plugins []string, maxAge time.Duration, progress io.Writer) (any, error)
	start, close step
	refresh      func(ctx context.Context, container, manifest string) (any, error)
	status       func(ctx context.Context) (any, error)
	workspaceRm  func(ctx context.Context, volume string, force bool) (any, error)
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
		case "version", "build", "start", "refresh", "close", "status", "workspace-rm":
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
	case command == "build" && len(args) >= 3 && args[1] == "--agent-pm" && isPath(args[2]) && buildOptions(args[3:]) != nil:
		o := buildOptions(args[3:])
		s, arg = func(ctx context.Context, pkg string, progress io.Writer) (any, error) {
			return a.build(ctx, pkg, o.plugins, o.maxAge, progress)
		}, args[2]
	case command == "start" && len(args) == 3 && args[1] == "--manifest" && isPath(args[2]):
		s, arg = a.start, args[2]
	case command == "refresh" && len(args) == 5 && args[1] == "--container" && containerPattern.MatchString(args[2]) &&
		args[3] == "--manifest" && isPath(args[4]):
		manifest := args[4]
		s, arg = func(ctx context.Context, container string, _ io.Writer) (any, error) {
			return a.refresh(ctx, container, manifest)
		}, args[2]
	case command == "status" && len(args) == 1:
		s = func(ctx context.Context, _ string, _ io.Writer) (any, error) { return a.status(ctx) }
	case command == "close" && len(args) == 3 && args[1] == "--container" && containerPattern.MatchString(args[2]):
		s, arg = a.close, args[2]
	case command == "workspace-rm" && (len(args) == 3 || len(args) == 4 && args[3] == "--force") && args[1] == "--volume" &&
		volumePattern.MatchString(args[2]):
		force := len(args) == 4
		s, arg = func(ctx context.Context, volume string, _ io.Writer) (any, error) {
			return a.workspaceRm(ctx, volume, force)
		}, args[2]
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

type buildArgs struct {
	plugins []string
	maxAge  time.Duration
}

// buildOptions parses build's [--plugin NAME]... [--max-age SECONDS], else nil.
func buildOptions(args []string) *buildArgs {
	o := &buildArgs{maxAge: -1}
	for len(args) >= 2 && args[0] == "--plugin" && pluginPattern.MatchString(args[1]) {
		o.plugins, args = append(o.plugins, args[1]), args[2:]
	}
	if len(args) == 2 && args[0] == "--max-age" && secondsPattern.MatchString(args[1]) {
		n, _ := strconv.Atoi(args[1])
		o.maxAge, args = time.Duration(n)*time.Second, nil
	}
	if len(args) != 0 {
		return nil
	}
	return o
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
	h := launcher.NewHost(home, d.Endpoint(), d, broker.Credentials{Keychain: broker.SystemKeychain{Home: home}}, tokens(home))
	h.Progress = progress
	return f(h)
}

// tokens is the broker's GitHub App token store in the account's login keychain.
func tokens(home string) broker.GitHub {
	k := broker.SystemKeychain{Home: home}
	return broker.GitHub{Keychain: k, Apps: k, API: github.New(), Lock: broker.FileLock(filepath.Join(home, ".agent-pm/docker/tokens.lock"))}
}

func system() app {
	return app{
		build: func(ctx context.Context, pkg string, plugins []string, maxAge time.Duration, progress io.Writer) (any, error) {
			return withHost(progress, func(h launcher.Host) (any, error) { return h.Build(ctx, pkg, plugins, maxAge) })
		},
		start: func(ctx context.Context, manifest string, progress io.Writer) (any, error) {
			return withHost(progress, func(h launcher.Host) (any, error) { return h.Start(ctx, manifest) })
		},
		refresh: func(ctx context.Context, container, manifest string) (any, error) {
			return withHost(nil, func(h launcher.Host) (any, error) { return h.Refresh(ctx, container, manifest) })
		},
		status: func(ctx context.Context) (any, error) {
			home, err := accountHome()
			if err != nil {
				return nil, err
			}
			return launcher.Host{GitHub: tokens(home)}.Status(ctx)
		},
		close: func(ctx context.Context, container string, _ io.Writer) (any, error) {
			return withHost(nil, func(h launcher.Host) (any, error) { return h.Close(ctx, container) })
		},
		workspaceRm: func(ctx context.Context, volume string, force bool) (any, error) {
			return withHost(nil, func(h launcher.Host) (any, error) { return h.WorkspaceRm(ctx, volume, force) })
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
			return broker.LoginGitHub(app, broker.SystemKeychain{Home: home}, func() ([]byte, error) {
				fmt.Fprint(os.Stderr, "Login keychain password, to make "+app+" trust only the broker (hidden): ")
				defer fmt.Fprintln(os.Stderr)
				return broker.ReadHidden()
			}, out)
		},
	}
}
