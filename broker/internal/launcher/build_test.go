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
	"time"

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

func inspectJSON(id string, labels map[string]string, created time.Time) []byte {
	data, _ := json.Marshal([]map[string]any{{"Id": id, "Created": created, "Config": map[string]any{"Labels": labels}}})
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
	// images answers image ls, KEY standing for the built key, and dangling its untagged listing;
	// containers and used answer container ls and inspect.
	images, dangling, containers, used string
	age                                time.Duration // of every inspected image
	listErr, rmErr                     error
	progress                           strings.Builder
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
			switch args[1] {
			case "ls":
				if slices.Contains(args, "--filter") {
					return []byte(e.dangling), nil
				}
				return []byte(strings.ReplaceAll(e.images, "KEY", e.key)), nil
			case "rm":
				return nil, e.rmErr
			}
			labels, ok := e.built[args[len(args)-1]]
			if !ok {
				return nil, noImage
			}
			return inspectJSON("sha256:"+strings.Repeat("a", 64), labels, time.Now().Add(-e.age)), nil
		case "container":
			if args[1] == "ls" {
				return []byte(e.containers), e.listErr
			}
			return []byte(e.used), nil
		}
		return nil, errors.New("unexpected " + strings.Join(args, " "))
	}}
	return e
}

func (e *buildEnv) host() Host { return Host{Home: e.home, Docker: e.fake, Progress: &e.progress} }

func TestBuildCacheAndStages(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, -1)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Rebuilt || first.Image != "agent-pm-worker:"+e.key || first.Digest != "sha256:"+strings.Repeat("a", 64) {
		t.Fatalf("%+v", first)
	}
	if got := e.fake.verbs(); !slices.Equal(got, []string{"image inspect", "build --progress=plain", "run --rm", "build --progress=plain", "image inspect", "container ls", "image ls", "image ls"}) {
		t.Fatalf("%q", got)
	}
	stage1 := e.fake.calls[1]
	if want := []string{"build", "--progress=plain", "--no-cache", "--label", "agent-pm.key=" + e.key, "--tag", "agent-pm-user:" + e.key}; !slices.Equal(stage1[:len(want)], want) || len(stage1) != len(want)+1 {
		t.Fatalf("%q", stage1)
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
	second, err := e.host().Build(context.Background(), e.pkg, -1)
	if err != nil || second.Rebuilt || second.Image != first.Image || len(e.fake.calls) != calls+1 {
		t.Fatalf("%+v %v %q", second, err, e.fake.verbs())
	}
	putFile(t, filepath.Join(e.pkg, "session/src/sample.py"), "changed")
	third, err := e.host().Build(context.Background(), e.pkg, -1)
	if err != nil || !third.Rebuilt || third.Image == first.Image {
		t.Fatalf("%+v %v", third, err)
	}
	putFile(t, filepath.Join(e.home, ".agent-pm/docker/worker/Dockerfile"), "FROM other")
	fourth, err := e.host().Build(context.Background(), e.pkg, -1)
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
			_, err := e.host().Build(context.Background(), e.pkg, -1)
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
				return inspectJSON("sha256:"+strings.Repeat("b", 64), map[string]string{"agent-pm.version": "9.9.9"}, time.Time{}), nil
			}
			return inner(args)
		}
		if _, err := e.host().Build(context.Background(), e.pkg, -1); code(err) != protocol.BuildFailed {
			t.Fatal(err)
		}
	})
	t.Run("unreachable", func(t *testing.T) {
		e := newBuildEnv(t)
		e.fake.respond = func([]string) ([]byte, error) { return nil, docker.ErrUnreachable }
		if _, err := e.host().Build(context.Background(), e.pkg, -1); code(err) != protocol.DockerUnreachable {
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

func TestWorkerImageHasTheRelayAndWorkspaceCheck(t *testing.T) {
	data, err := assets.ReadFile("assets/Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`'exec /opt/agent-pm/venv/bin/python -I /opt/agent-pm/session/src/agent_pm/session/hosts/docker/relay.py "$@"' > /opt/agent-pm/bin/relay`,
		"chmod 755 /opt/agent-pm/bin/agent-pm-session /opt/agent-pm/bin/relay ",
		"install -d -o agent -g agent -m 0700 /run/agent-pm /run/agent-pm/relay-queue",
		"COPY bootstrap.py hook-stub with-secrets workspace-check /opt/agent-pm/lib/",
		" /opt/agent-pm/lib/workspace-check \\\n",
	} {
		if !strings.Contains(string(data), want) {
			t.Errorf("Dockerfile lacks %q", want)
		}
	}
}

func TestBuildContextOutsideTMPDIR(t *testing.T) {
	e := newBuildEnv(t)
	t.Setenv("TMPDIR", t.TempDir())
	if _, err := e.host().Build(context.Background(), e.pkg, -1); err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(e.home, ".agent-pm/docker/tmp") + "/"
	for _, i := range []int{1, 3} {
		if dir := e.fake.calls[i][len(e.fake.calls[i])-1]; !strings.HasPrefix(dir, base) {
			t.Fatalf("build context %s outside %s", dir, base)
		}
	}
}

func TestBuildRemovesOlderImages(t *testing.T) {
	old, kept, id := strings.Repeat("1", 64), strings.Repeat("2", 64), func(c string) string { return "sha256:" + strings.Repeat(c, 64) }
	listing := strings.Join([]string{
		"agent-pm-worker:KEY " + id("a"), "agent-pm-user:KEY " + id("b"),
		"agent-pm-worker:" + old + " " + id("c"), "agent-pm-user:" + old + " " + id("d"),
		"agent-pm-worker:" + kept + " " + id("e"), "agent-pm-user:" + kept + " " + id("f"),
		"agent-pm-worker:latest " + id("7"), "ubuntu:24.04 " + id("8"), "<none>:<none> " + id("9"), "",
	}, "\n")
	removed := func(e *buildEnv) []string {
		var refs []string
		for _, c := range e.fake.calls {
			if strings.Join(c[:2], " ") == "image rm" {
				refs = append(refs, c[2:]...)
			}
		}
		return refs
	}
	t.Run("keeps the current pair and images containers use", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.containers, e.used = listing, "c1\nc2\n", id("e")+"\n"+id("0")+"\n"
		if _, err := e.host().Build(context.Background(), e.pkg, -1); err != nil {
			t.Fatal(err)
		}
		want := []string{"agent-pm-worker:" + old, "agent-pm-user:" + old, "agent-pm-user:" + kept}
		if got := removed(e); !slices.Equal(got, want) {
			t.Fatalf("%q", got)
		}
		inspect := e.fake.calls[slices.Index(e.fake.verbs(), "container inspect")]
		if !slices.Equal(inspect, []string{"container", "inspect", "--format", "{{.Image}}", "c1", "c2"}) {
			t.Fatalf("%q", inspect)
		}
		if got := e.progress.String(); got != "agent-pm-broker: removed agent-pm-worker:"+old+"\nagent-pm-broker: removed agent-pm-user:"+old+"\nagent-pm-broker: removed agent-pm-user:"+kept+"\n" {
			t.Fatalf("%q", got)
		}
		calls := len(e.fake.calls)
		if again, err := e.host().Build(context.Background(), e.pkg, -1); err != nil || again.Rebuilt || len(e.fake.calls) != calls+1 {
			t.Fatalf("a reused image pruned: %v %q", err, e.fake.verbs()[calls:])
		}
	})
	t.Run("a failed removal keeps the image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.rmErr = listing, &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: conflict: image is in use"}
		if _, err := e.host().Build(context.Background(), e.pkg, -1); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(e.progress.String(), "agent-pm-broker: kept agent-pm-worker:"+old+": Error response from daemon: conflict: image is in use\n") {
			t.Fatalf("%q", e.progress.String())
		}
	})
	t.Run("unknown containers keep every image", func(t *testing.T) {
		e := newBuildEnv(t)
		e.images, e.listErr = listing, &docker.CommandError{Exit: 1, Stderr: "boom"}
		if _, err := e.host().Build(context.Background(), e.pkg, -1); err != nil {
			t.Fatal(err)
		}
		if got := removed(e); got != nil || e.progress.String() != "agent-pm-broker: kept older images: cannot list containers: boom\n" {
			t.Fatalf("%q %q", got, e.progress.String())
		}
	})
}

func TestBuildRefreshesAnOldImage(t *testing.T) {
	const week = 7 * 24 * time.Hour
	e := newBuildEnv(t)
	if _, err := e.host().Build(context.Background(), e.pkg, -1); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name    string
		age     time.Duration
		maxAge  time.Duration
		rebuilt bool
	}{
		{"young", 6 * 24 * time.Hour, week, false},
		{"old", 8 * 24 * time.Hour, week, true},
		{"any age", 400 * 24 * time.Hour, -1, false},
		{"forced", time.Minute, 0, true},
		{"built in the future", -time.Hour, week, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e.age = tc.age
			calls := len(e.fake.calls)
			got, err := e.host().Build(context.Background(), e.pkg, tc.maxAge)
			if err != nil || got.Rebuilt != tc.rebuilt || got.Image != "agent-pm-worker:"+e.key {
				t.Fatalf("%+v %v", got, err)
			}
			if !tc.rebuilt {
				if len(e.fake.calls) != calls+1 {
					t.Fatalf("%q", e.fake.verbs()[calls:])
				}
				return
			}
			if stage1 := e.fake.calls[calls+1]; stage1[0] != "build" || stage1[2] != "--no-cache" {
				t.Fatalf("a refresh may reuse cached clients: %q", stage1)
			}
		})
	}
}

func TestBuildRemovesUntaggedImages(t *testing.T) {
	id := func(c string) string { return "sha256:" + strings.Repeat(c, 64) }
	e := newBuildEnv(t)
	e.dangling, e.containers, e.used = id("3")+"\n"+id("4")+"\n", "c1\n", id("4")+"\n"
	if _, err := e.host().Build(context.Background(), e.pkg, -1); err != nil {
		t.Fatal(err)
	}
	listing := e.fake.calls[len(e.fake.calls)-2]
	if !slices.Equal(listing, []string{"image", "ls", "--quiet", "--no-trunc", "--filter", "dangling=true", "--filter", "label=agent-pm.key"}) {
		t.Fatalf("%q", listing)
	}
	if rm := e.fake.calls[len(e.fake.calls)-1]; !slices.Equal(rm, []string{"image", "rm", id("3")}) {
		t.Fatalf("%q", rm)
	}
	if got := e.progress.String(); got != "agent-pm-broker: removed "+id("3")+"\n" {
		t.Fatalf("%q", got)
	}
}

func TestAFailedRefreshKeepsTheImage(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, -1)
	if err != nil {
		t.Fatal(err)
	}
	e.age, e.failStage = 8*24*time.Hour, "agent-pm-user"
	e.progress.Reset()
	got, err := e.host().Build(context.Background(), e.pkg, 7*24*time.Hour)
	if err != nil || got != (BuildResult{Image: first.Image, Digest: first.Digest}) {
		t.Fatalf("%+v %v", got, err)
	}
	if want := "agent-pm-broker: kept " + first.Image + ": refreshing it failed: Building the worker image definition failed. (RUN false)\n"; e.progress.String() != want {
		t.Fatalf("%q", e.progress.String())
	}
	if _, err := e.host().Build(context.Background(), e.pkg, 0); err == nil {
		t.Fatal("a forced rebuild kept the image")
	}
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" {
			return nil, docker.ErrUnreachable
		}
		return inner(args)
	}
	if _, err := e.host().Build(context.Background(), e.pkg, 7*24*time.Hour); err == nil || err.(*protocol.Error).Code != protocol.DockerUnreachable {
		t.Fatalf("unreachable Docker kept the image: %v", err)
	}
}
