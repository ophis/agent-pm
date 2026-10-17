package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type fakeDocker struct {
	calls   [][]string
	stdin   [][]byte
	ctxErrs []error
	respond func(args []string) ([]byte, error)
}

func (f *fakeDocker) Run(ctx context.Context, args []string, stdin []byte) ([]byte, error) {
	f.calls = append(f.calls, slices.Clone(args))
	f.stdin = append(f.stdin, slices.Clone(stdin))
	f.ctxErrs = append(f.ctxErrs, ctx.Err())
	if f.respond != nil {
		return f.respond(args)
	}
	return nil, nil
}

func (f *fakeDocker) verbs() []string {
	var out []string
	for _, c := range f.calls {
		out = append(out, strings.Join(c[:min(2, len(c))], " "))
	}
	return out
}

func putFile(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func inspectJSON(id string, labels map[string]string) []byte {
	data, _ := json.Marshal([]map[string]any{{"Id": id, "Config": map[string]any{"Labels": labels}}})
	return data
}

var noImage = &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: No such image: x"}

func TestSnapshotKey(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM base\n")
	putFile(t, filepath.Join(src, "__pycache__/x.pyc"), "junk")
	putFile(t, filepath.Join(src, "mod.pyc"), "junk")
	if err := os.Symlink("Dockerfile", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(filepath.Join(dst, "__pycache__")); !os.IsNotExist(err) {
		t.Fatal("pycache copied")
	}
	a, err := treeDigest(dst)
	if err != nil {
		t.Fatal(err)
	}
	putFile(t, filepath.Join(src, "Dockerfile"), "FROM changed\n")
	if b, _ := treeDigest(dst); a != b {
		t.Fatal("snapshot changed after source write")
	}
	if err := snapshotTree(src, dst); err != nil {
		t.Fatal(err)
	}
	b, _ := treeDigest(dst)
	if a == b {
		t.Fatal("content did not change key")
	}
	if err := os.Chmod(filepath.Join(dst, "Dockerfile"), 0o755); err != nil {
		t.Fatal(err)
	}
	if c, _ := treeDigest(dst); b == c {
		t.Fatal("mode did not change key")
	}
}

type buildEnv struct {
	home, pkg string
	fake      *fakeDocker
	key       string
	built     map[string]map[string]string
	probe     string
	failStage string
}

func newBuildEnv(t *testing.T) *buildEnv {
	e := &buildEnv{home: t.TempDir(), pkg: t.TempDir(), built: map[string]map[string]string{}, probe: "1001\n1002\n"}
	putFile(t, filepath.Join(e.home, ".agent-pm/docker/worker/Dockerfile"), "FROM base")
	putFile(t, filepath.Join(e.pkg, ".claude-plugin/plugin.json"), `{"version":"1.2.3"}`)
	putFile(t, filepath.Join(e.pkg, "session/src/sample.py"), "session")
	putFile(t, filepath.Join(e.pkg, "utils/src/sample.py"), "utils")
	e.fake = &fakeDocker{respond: func(args []string) ([]byte, error) {
		switch args[0] {
		case "build":
			tag := args[slices.Index(args, "--tag")+1]
			if strings.HasPrefix(tag, e.failStage+":") && e.failStage != "" {
				return nil, &docker.CommandError{Exit: 1, Stderr: "step 3 failed\nRUN false"}
			}
			labels := map[string]string{}
			for i, a := range args {
				if a == "--label" {
					k, v, _ := strings.Cut(args[i+1], "=")
					labels[k] = v
				}
				if a == "--build-arg" {
					k, v, _ := strings.Cut(args[i+1], "=")
					switch k {
					case "AGENT_PM_VERSION":
						labels["agent-pm.version"] = v
					case "AGENT_UID":
						labels["agent-pm.agent-uid"] = v
					case "AGENT_GID":
						labels["agent-pm.agent-gid"] = v
					}
				}
			}
			e.built[tag] = labels
			if k, ok := strings.CutPrefix(tag, "agent-pm-worker:"); ok {
				e.key = k
			}
			return nil, nil
		case "run":
			return []byte(e.probe), nil
		case "image":
			labels, ok := e.built[args[len(args)-1]]
			if !ok {
				return nil, noImage
			}
			return inspectJSON("sha256:"+strings.Repeat("a", 64), labels), nil
		}
		return nil, errors.New("unexpected " + strings.Join(args, " "))
	}}
	return e
}

func (e *buildEnv) host() Host { return Host{Home: e.home, Docker: e.fake} }

func TestBuildCacheAndStages(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Rebuilt || first.Image != "agent-pm-worker:"+e.key || first.Digest != "sha256:"+strings.Repeat("a", 64) {
		t.Fatalf("%+v", first)
	}
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect", "build --progress=plain", "run --rm", "build --progress=plain", "image inspect"}) {
		t.Fatalf("%q", got)
	}
	probe := e.fake.calls[2]
	wantProbe := []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "/bin/sh", "agent-pm-user:" + e.key, "-c", "id -u agent && id -g agent"}
	if !slices.Equal(probe, wantProbe) {
		t.Fatalf("%q", probe)
	}
	stage2 := e.fake.calls[3]
	want := []string{"build", "--progress=plain", "--build-arg", "USER_IMAGE=agent-pm-user:" + e.key, "--build-arg", "AGENT_PM_VERSION=1.2.3", "--build-arg", "AGENT_UID=1001", "--build-arg", "AGENT_GID=1002", "--label", "agent-pm.key=" + e.key, "--tag", "agent-pm-worker:" + e.key}
	if !slices.Equal(stage2[:len(want)], want) || len(stage2) != len(want)+1 {
		t.Fatalf("%q", stage2)
	}
	for _, c := range e.fake.calls {
		for _, a := range c {
			if a == "--secret" || a == "--ssh" {
				t.Fatalf("forbidden flag in %q", c)
			}
		}
	}
	calls := len(e.fake.calls)
	second, err := e.host().Build(context.Background(), e.pkg)
	if err != nil || second.Rebuilt || second.Image != first.Image || len(e.fake.calls) != calls+1 {
		t.Fatalf("%+v %v %q", second, err, e.fake.verbs())
	}
	putFile(t, filepath.Join(e.pkg, "session/src/sample.py"), "changed")
	third, err := e.host().Build(context.Background(), e.pkg)
	if err != nil || !third.Rebuilt || third.Image == first.Image {
		t.Fatalf("%+v %v", third, err)
	}
	putFile(t, filepath.Join(e.home, ".agent-pm/docker/worker/Dockerfile"), "FROM other")
	fourth, err := e.host().Build(context.Background(), e.pkg)
	if err != nil || !fourth.Rebuilt || fourth.Image == third.Image {
		t.Fatalf("%+v %v", fourth, err)
	}
}

func TestBuildFailures(t *testing.T) {
	code := func(err error) protocol.Code {
		var e *protocol.Error
		if !errors.As(err, &e) {
			t.Fatalf("not a protocol error: %v", err)
		}
		return e.Code
	}
	for name, mutate := range map[string]func(*buildEnv){
		"missing definition": func(e *buildEnv) { os.RemoveAll(filepath.Join(e.home, ".agent-pm")) },
		"missing version":    func(e *buildEnv) { os.Remove(filepath.Join(e.pkg, ".claude-plugin/plugin.json")) },
		"bad version":        func(e *buildEnv) { putFile(t, filepath.Join(e.pkg, ".claude-plugin/plugin.json"), `{"version":"v1"}`) },
		"missing session":    func(e *buildEnv) { os.RemoveAll(filepath.Join(e.pkg, "session")) },
		"root agent":         func(e *buildEnv) { e.probe = "0\n0\n" },
		"probe garbage":      func(e *buildEnv) { e.probe = "agent\n" },
		"stage one":          func(e *buildEnv) { e.failStage = "agent-pm-user" },
		"stage two":          func(e *buildEnv) { e.failStage = "agent-pm-worker" },
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			mutate(e)
			_, err := e.host().Build(context.Background(), e.pkg)
			if code(err) != protocol.BuildFailed {
				t.Fatal(err)
			}
			if name == "missing definition" && !strings.Contains(err.(*protocol.Error).Message, "broker/install.sh") {
				t.Fatal(err)
			}
			if name == "stage one" && !strings.Contains(err.(*protocol.Error).Cause, "RUN false") {
				t.Fatalf("cause %q", err.(*protocol.Error).Cause)
			}
		})
	}
	t.Run("label mismatch", func(t *testing.T) {
		e := newBuildEnv(t)
		inner := e.fake.respond
		e.fake.respond = func(args []string) ([]byte, error) {
			if args[0] == "image" && len(e.built) == 2 {
				return inspectJSON("sha256:"+strings.Repeat("b", 64), map[string]string{"agent-pm.version": "9.9.9"}), nil
			}
			return inner(args)
		}
		if _, err := e.host().Build(context.Background(), e.pkg); code(err) != protocol.BuildFailed {
			t.Fatal(err)
		}
	})
	t.Run("unreachable", func(t *testing.T) {
		e := newBuildEnv(t)
		e.fake.respond = func([]string) ([]byte, error) { return nil, docker.ErrUnreachable }
		if _, err := e.host().Build(context.Background(), e.pkg); code(err) != protocol.DockerUnreachable {
			t.Fatal(err)
		}
	})
}

func TestImageProvenance(t *testing.T) {
	for name, w := range map[string]*world{
		"absent":     {imageMissing: true},
		"no key":     {labels: map[string]string{"agent-pm.version": "1", "agent-pm.agent-uid": "1", "agent-pm.agent-gid": "1"}},
		"wrong key":  {labels: map[string]string{"agent-pm.key": strings.Repeat("c", 64), "agent-pm.version": "1", "agent-pm.agent-uid": "1", "agent-pm.agent-gid": "1"}},
		"bad uid":    {labels: map[string]string{"agent-pm.key": imageKey, "agent-pm.version": "1", "agent-pm.agent-uid": "x", "agent-pm.agent-gid": "1"}},
		"no version": {labels: map[string]string{"agent-pm.key": imageKey, "agent-pm.agent-uid": "1", "agent-pm.agent-gid": "1"}},
	} {
		t.Run(name, func(t *testing.T) {
			h, d := startHost(w, &fakeCreds{})
			_, err := h.Start(context.Background(), validManifest(t))
			if protoErr(t, err).Code != protocol.ImageRejected || hasCall(d, "run") {
				t.Fatalf("%v %q", err, d.verbs())
			}
		})
	}
}

func TestBuildContextOutsideTMPDIR(t *testing.T) {
	e := newBuildEnv(t)
	t.Setenv("TMPDIR", t.TempDir())
	if _, err := e.host().Build(context.Background(), e.pkg); err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(e.home, ".agent-pm/docker/tmp") + "/"
	for _, i := range []int{1, 3} {
		if dir := e.fake.calls[i][len(e.fake.calls[i])-1]; !strings.HasPrefix(dir, base) {
			t.Fatalf("build context %s outside %s", dir, base)
		}
	}
}
