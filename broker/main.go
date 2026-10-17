package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
)

const protocol = "1"
const installedPath = "/usr/local/libexec/agent-pm/agent-pm-broker"
const upgradeCommand = "sh broker/install.sh (from the current agent-pm package)"

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if err := command(ctx, os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "agent-pm-broker:", err)
		os.Exit(1)
	}
}

func command(ctx context.Context, args []string, out io.Writer) error {
	if len(args) == 1 && args[0] == "version" {
		_, err := fmt.Fprintln(out, protocol)
		return err
	}
	if len(args) == 3 && args[0] == "build" && args[1] == "--agent-pm" {
		home, err := accountHome()
		if err != nil {
			return err
		}
		d, clean, err := newDocker(home)
		if err != nil {
			return err
		}
		defer clean()
		result, err := (builder{home: home, docker: d}).Build(ctx, args[2])
		if err != nil {
			return err
		}
		return json.NewEncoder(out).Encode(result)
	}
	if len(args) == 2 && args[0] == "login" && args[1] == "claude" {
		if err := checkInstalled(); err != nil {
			return err
		}
		fmt.Fprint(os.Stderr, "Claude setup-token (hidden): ")
		token, err := terminalToken()
		fmt.Fprintln(os.Stderr)
		if err != nil {
			return err
		}
		defer erase(token)
		if len(token) == 0 || len(token) > 16384 || strings.ContainsAny(string(token), "\r\n\x00") {
			return errors.New("invalid setup-token")
		}
		if err := (keychainStore{}).Put(token); err != nil {
			return err
		}
		_, err = fmt.Fprintln(out, "Claude setup-token saved")
		return err
	}
	return errors.New("usage: agent-pm-broker version | login claude | build --agent-pm DIR")
}

func erase(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

type tokenStore interface {
	Get() ([]byte, error)
	Put([]byte) error
}

type keychainStore struct{}
