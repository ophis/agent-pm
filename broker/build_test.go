package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakeRunner struct {
	calls   [][]string
	inputs  [][]byte
	respond func([]string, []byte) ([]byte, error)
}

func (f *fakeRunner) Run(_ context.Context, args []string, input []byte) ([]byte, error) {
	f.calls = append(f.calls, append([]string(nil), args...))
	f.inputs = append(f.inputs, append([]byte(nil), input...))
	if f.respond != nil {
		return f.respond(args, input)
	}
	return nil, nil
}

func putFile(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0644); err != nil {
		t.Fatal(err)
	}
}

func TestSnapshotHash(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM base\n")
	if err := os.Symlink("Dockerfile", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	a, err := treeDigest(dst)
	if err != nil {
		t.Fatal(err)
	}
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM changed\n")
	b, _ := treeDigest(dst)
	if a != b {
		t.Fatal("snapshot changed after source write")
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	b, _ = treeDigest(dst)
	if a == b {
		t.Fatal("content did not change hash")
	}
	if err := os.Chmod(filepath.Join(dst, "Dockerfile"), 0755); err != nil {
		t.Fatal(err)
	}
	c, _ := treeDigest(dst)
	if b == c {
		t.Fatal("mode did not change hash")
	}
}

func TestBuildCacheAndStages(t *testing.T) {
	home, pkg := t.TempDir(), t.TempDir()
	putFile(t, filepath.Join(home, ".agent-pm/docker/worker/Dockerfile"), "FROM base")
	putFile(t, filepath.Join(pkg, ".claude-plugin/plugin.json"), `{"version":"1.2.3"}`)
	putFile(t, filepath.Join(pkg, "session/src/sample.py"), "session")
	putFile(t, filepath.Join(pkg, "utils/src/sample.py"), "utils")
	id := "sha256:" + strings.Repeat("a", 64)
	var key string
	builds := 0
	fake := &fakeRunner{respond: func(args []string, _ []byte) ([]byte, error) {
		if args[0] == "build" {
			builds++
			for _, a := range args {
				if strings.HasPrefix(a, "agent-pm-worker:") {
					key = strings.TrimPrefix(a, "agent-pm-worker:")
				}
			}
			return nil, nil
		}
		if key == "" && strings.HasPrefix(args[len(args)-1], "agent-pm-worker:") {
			return nil, errors.New("not found")
		}
		return json.Marshal([]imageInfo{{ID: id, Config: imageConfig{Labels: map[string]string{"agent-pm.version": "1.2.3", "agent-pm.key": key}}}})
	}}
	b := builder{home: home, docker: fake}
	first, err := b.Build(context.Background(), pkg)
	if err != nil {
		t.Fatal(err)
	}
	second, err := b.Build(context.Background(), pkg)
	if err != nil || !first.Rebuilt || second.Rebuilt || first.Image != second.Image || builds != 2 {
		t.Fatalf("%+v %+v builds=%d err=%v", first, second, builds, err)
	}
	putFile(t, filepath.Join(pkg, "session/src/sample.py"), "changed")
	third, err := b.Build(context.Background(), pkg)
	if err != nil || !third.Rebuilt || third.Image == first.Image {
		t.Fatalf("%+v %v", third, err)
	}
	data, _ := json.Marshal(first)
	for _, field := range []string{`"image"`, `"digest"`, `"rebuilt"`} {
		if !strings.Contains(string(data), field) {
			t.Fatal(string(data))
		}
	}
}

func TestBuildRejectsMissingVersion(t *testing.T) {
	home, pkg := t.TempDir(), t.TempDir()
	putFile(t, filepath.Join(home, ".agent-pm/docker/worker/Dockerfile"), "FROM base")
	if _, err := (builder{home: home, docker: &fakeRunner{}}).Build(context.Background(), pkg); err == nil {
		t.Fatal("missing package accepted")
	}
}
