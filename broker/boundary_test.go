package main

import (
	"context"
	"reflect"
	"strings"
	"testing"
)

func TestDockerBoundary(t *testing.T) {
	t.Setenv("HOME", "/hostile")
	t.Setenv("DOCKER_HOST", "tcp://hostile:1234")
	t.Setenv("DOCKER_CONTEXT", "hostile")
	t.Setenv("PATH", "/hostile")
	d := dockerCLI{home: "/Users/test", config: "/private/config"}
	path, args, env := d.command([]string{"inspect", "image"})
	if path != dockerPath || !reflect.DeepEqual(args, []string{"--config", "/private/config", "--host", "unix:///Users/test/.docker/run/docker.sock", "inspect", "image"}) {
		t.Fatalf("unexpected command %s %q", path, args)
	}
	for _, v := range env {
		if strings.Contains(v, "hostile") || strings.HasPrefix(v, "DOCKER_CONTEXT=") {
			t.Fatalf("ambient environment leaked: %s", v)
		}
	}
	if d.endpoint() != "unix:///Users/test/.docker/run/docker.sock" {
		t.Fatal(d.endpoint())
	}
}

func TestCommandParsing(t *testing.T) {
	var output strings.Builder
	if err := command(context.Background(), []string{"version"}, &output); err != nil || output.String() != "1\n" {
		t.Fatalf("version: %q %v", output.String(), err)
	}
	for _, args := range [][]string{nil, {"version", "extra"}, {"login"}, {"login", "codex"}, {"unknown"}} {
		if command(context.Background(), args, &output) == nil {
			t.Fatalf("accepted %q", args)
		}
	}
}
