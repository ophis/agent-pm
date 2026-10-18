package codex

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func archive(t *testing.T, entries ...*tar.Header) []byte {
	t.Helper()
	var b bytes.Buffer
	z := gzip.NewWriter(&b)
	tw := tar.NewWriter(z)
	for _, h := range entries {
		if err := tw.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg && h.Size > 0 {
			if _, err := tw.Write(bytes.Repeat([]byte("x"), int(h.Size))); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func fixture(t *testing.T) (Runtime, map[string][]byte) {
	t.Helper()
	r := New()
	r.root = filepath.Join(t.TempDir(), "codex")
	r.trustRoot = filepath.Dir(r.root)
	r.tempRoot = t.TempDir()
	r.uid = func() int { return 0 }
	r.platform = func() (string, string) { return "darwin", "arm64" }
	r.owned = func(os.FileInfo) bool { return true }
	r.execute = func(_ context.Context, _ command) ([]byte, error) { return []byte("codex-cli 0.162.1\n"), nil }
	main := archive(t, &tar.Header{Name: "package/package.json", Typeflag: tar.TypeReg, Mode: 0644, Size: 1})
	native := archive(t, &tar.Header{Name: nativeEntry, Typeflag: tar.TypeReg, Mode: 0755, Size: 6})
	data := map[string][]byte{"https://registry.npmjs.org/main.tgz": main, "https://registry.npmjs.org/native.tgz": native}
	metadata := func(name, version, tarball string, body []byte, extra map[string]any) []byte {
		sum := sha512.Sum512(body)
		m := map[string]any{"name": name, "version": version, "dist": map[string]string{"tarball": tarball, "integrity": "sha512-" + base64.StdEncoding.EncodeToString(sum[:])}}
		for k, v := range extra {
			m[k] = v
		}
		b, _ := json.Marshal(m)
		return b
	}
	data["https://registry.npmjs.org/@openai%2Fcodex/0.162.1"] = metadata("@openai/codex", "0.162.1", "https://registry.npmjs.org/main.tgz", main, map[string]any{"optionalDependencies": map[string]string{"@openai/codex-darwin-arm64": "npm:@openai/codex@0.162.1-darwin-arm64"}})
	data["https://registry.npmjs.org/@openai%2Fcodex/0.162.1-darwin-arm64"] = metadata("@openai/codex", "0.162.1-darwin-arm64", "https://registry.npmjs.org/native.tgz", native, map[string]any{"os": []string{"darwin"}, "cpu": []string{"arm64"}})
	r.http = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		body, ok := data[req.URL.String()]
		if !ok {
			t.Fatalf("unexpected registry request: %s", req.URL.String())
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(body)), Header: make(http.Header), Request: req}, nil
	})}
	return r, data
}

func TestInstallAndVerify(t *testing.T) {
	r, _ := fixture(t)
	got, err := r.Install(context.Background(), "0.162.1", false)
	if err != nil {
		t.Fatal(err)
	}
	if got.Version != "0.162.1" || len(got.SHA256) != 64 || len(got.AllowedVersions) != 0 || !filepath.IsAbs(got.Path) {
		t.Fatalf("bad installation: %+v", got)
	}
	verified, err := r.Verify()
	if err != nil || !reflect.DeepEqual(got, verified) {
		t.Fatalf("verify: %+v %v", verified, err)
	}
	if _, err := r.Install(context.Background(), "0.162.1", false); err == nil {
		t.Fatal("reinstall accepted")
	}
	lock, err := os.Stat(filepath.Join(r.root, "auth.lock"))
	if err != nil || lock.Mode().Perm() != 0644 {
		t.Fatalf("lock mode: %v %v", lock, err)
	}
}

type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) {
	clear(p)
	return len(p), nil
}

func TestPublishedPlatformArchiveSize(t *testing.T) {
	var compressed bytes.Buffer
	z := gzip.NewWriter(&compressed)
	tw := tar.NewWriter(z)
	const publishedSize = 338137646
	if err := tw.WriteHeader(&tar.Header{Name: "package/support-data", Typeflag: tar.TypeReg, Mode: 0644, Size: publishedSize}); err != nil {
		t.Fatal(err)
	}
	if _, err := io.CopyN(tw, zeroReader{}, publishedSize); err != nil {
		t.Fatal(err)
	}
	if err := tw.WriteHeader(&tar.Header{Name: nativeEntry, Typeflag: tar.TypeReg, Mode: 0755, Size: 6}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write([]byte("native")); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	got, err := readArchive(compressed.Bytes(), true)
	if err != nil || string(got) != "native" {
		t.Fatalf("published size rejected: %v", err)
	}
}

func TestIntegrityMismatchPreservesInstallation(t *testing.T) {
	for _, suffix := range []string{"0.162.1", "0.162.1-darwin-arm64"} {
		t.Run(suffix, func(t *testing.T) {
			r, data := fixture(t)
			if _, err := r.Install(context.Background(), "0.162.1", false); err != nil {
				t.Fatal(err)
			}
			before, err := os.ReadFile(filepath.Join(r.root, "active.json"))
			if err != nil {
				t.Fatal(err)
			}
			url := "https://registry.npmjs.org/@openai%2Fcodex/" + suffix
			var meta map[string]any
			if err := json.Unmarshal(data[url], &meta); err != nil {
				t.Fatal(err)
			}
			meta["dist"].(map[string]any)["integrity"] = "sha512-" + base64.StdEncoding.EncodeToString(make([]byte, 64))
			data[url], _ = json.Marshal(meta)
			if _, err := r.Install(context.Background(), "0.162.1", true); err == nil || !strings.Contains(err.Error(), "integrity mismatch") {
				t.Fatalf("wrong integrity result: %v", err)
			}
			after, err := os.ReadFile(filepath.Join(r.root, "active.json"))
			if err != nil || !bytes.Equal(before, after) {
				t.Fatal("failed integrity replaced active installation")
			}
			if _, err := r.Verify(); err != nil {
				t.Fatal(err)
			}
			fresh, _ := fixture(t)
			fresh.http = r.http
			if _, err := fresh.Install(context.Background(), "0.162.1", false); err == nil || !strings.Contains(err.Error(), "integrity mismatch") {
				t.Fatalf("fresh install accepted mismatching integrity: %v", err)
			}
			if _, err := os.Stat(filepath.Join(fresh.root, "active.json")); !os.IsNotExist(err) {
				t.Fatal("failed fresh install published metadata")
			}
		})
	}
}

func TestArchiveTotalSizeBound(t *testing.T) {
	body := archive(t, &tar.Header{Name: "package/one", Typeflag: tar.TypeReg, Mode: 0644, Size: 1024}, &tar.Header{Name: "package/two", Typeflag: tar.TypeReg, Mode: 0644, Size: 1024})
	if _, err := readArchiveBounded(body, false, 4096); err != nil {
		t.Fatalf("exact total size refused: %v", err)
	}
	if _, err := readArchiveBounded(body, false, 4095); err == nil {
		t.Fatal("total expanded size exceeded bound")
	}
}

func TestInstallGuards(t *testing.T) {
	for _, version := range []string{"latest", "v0.162.1", "0.162", "0.162.1-beta", "0.0162.1", "0.162.1/evil", "0.162.1\n"} {
		t.Run(version, func(t *testing.T) {
			if ValidVersion(version) {
				t.Fatal("invalid version accepted by CLI validator")
			}
			r, _ := fixture(t)
			if _, err := r.Install(context.Background(), version, false); err == nil {
				t.Fatal("invalid version accepted")
			}
		})
	}
	for _, name := range []string{"user", "platform", "missing update", "wrong native version"} {
		t.Run(name, func(t *testing.T) {
			r, _ := fixture(t)
			update := false
			switch name {
			case "user":
				r.uid = func() int { return 501 }
			case "platform":
				r.platform = func() (string, string) { return "darwin", "amd64" }
			case "missing update":
				update = true
			case "wrong native version":
				r.execute = func(context.Context, command) ([]byte, error) { return []byte("codex-cli 0.1.0"), nil }
			}
			if _, err := r.Install(context.Background(), "0.162.1", update); err == nil {
				t.Fatal("guard accepted")
			}
			if _, err := os.Stat(filepath.Join(r.root, "active.json")); !os.IsNotExist(err) {
				t.Fatal("failed install published")
			}
		})
	}
}

func TestNativeProbeIsPrivate(t *testing.T) {
	r, _ := fixture(t)
	var home string
	r.execute = func(_ context.Context, c command) ([]byte, error) {
		home = c.dir
		if filepath.Dir(home) != r.tempRoot {
			t.Fatalf("probe inherited temporary path: %s", home)
		}
		fi, err := os.Stat(home)
		if err != nil || fi.Mode().Perm() != 0700 {
			t.Fatalf("probe home permissions: %v %v", fi, err)
		}
		if !reflect.DeepEqual(c.env, environment(home)) || !reflect.DeepEqual(c.args, []string{"--version"}) {
			t.Fatalf("unsafe probe: %+v", c)
		}
		return []byte("codex-cli 0.162.1"), nil
	}
	if _, err := r.Install(context.Background(), "0.162.1", false); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(home); !os.IsNotExist(err) {
		t.Fatal("probe home retained")
	}
}

func TestRegistryValidation(t *testing.T) {
	for _, name := range []string{"integrity", "origin", "alias", "package", "cpu", "os", "redirect", "oversize"} {
		t.Run(name, func(t *testing.T) {
			r, data := fixture(t)
			key := "https://registry.npmjs.org/@openai%2Fcodex/0.162.1-darwin-arm64"
			var m map[string]any
			if err := json.Unmarshal(data[key], &m); err != nil {
				t.Fatal(err)
			}
			switch name {
			case "integrity":
				m["dist"].(map[string]any)["integrity"] = "sha512-AAAA"
			case "origin":
				m["dist"].(map[string]any)["tarball"] = "https://evil.example/package.tgz"
			case "package":
				m["name"] = "@evil/codex"
			case "cpu":
				m["cpu"] = []string{"x64"}
			case "os":
				m["os"] = []string{"linux"}
			case "alias":
				data["https://registry.npmjs.org/@openai%2Fcodex/0.162.1"] = bytes.ReplaceAll(data["https://registry.npmjs.org/@openai%2Fcodex/0.162.1"], []byte("npm:@openai/codex@0.162.1-darwin-arm64"), []byte("npm:@evil/codex@0.162.1"))
			case "redirect":
				r.http.Transport = roundTrip(func(req *http.Request) (*http.Response, error) {
					return &http.Response{StatusCode: 302, Header: http.Header{"Location": []string{"https://evil.example/"}}, Body: io.NopCloser(strings.NewReader("")), Request: req}, nil
				})
			case "oversize":
				data[key] = bytes.Repeat([]byte(" "), maxMetadata+1)
			}
			if name != "oversize" {
				data[key], _ = json.Marshal(m)
			}
			if _, err := r.Install(context.Background(), "0.162.1", false); err == nil {
				t.Fatal("bad registry metadata accepted")
			}
		})
	}
}

func TestUnsafeArchives(t *testing.T) {
	for _, h := range []*tar.Header{
		{Name: "../escape", Typeflag: tar.TypeReg, Mode: 0644},
		{Name: "/absolute", Typeflag: tar.TypeReg, Mode: 0644},
		{Name: "package/../escape", Typeflag: tar.TypeReg, Mode: 0644},
		{Name: "package\\escape", Typeflag: tar.TypeReg, Mode: 0644},
		{Name: "package/link", Typeflag: tar.TypeSymlink, Linkname: "target"},
		{Name: "package/link", Typeflag: tar.TypeLink, Linkname: nativeEntry},
		{Name: "package/device", Typeflag: tar.TypeChar},
		{Name: nativeEntry, Typeflag: tar.TypeReg, Mode: 0755, Size: 1},
	} {
		t.Run(h.Name+string(h.Typeflag), func(t *testing.T) {
			body := archive(t, &tar.Header{Name: nativeEntry, Typeflag: tar.TypeReg, Mode: 0755, Size: 1}, h)
			if _, err := readArchive(body, true); err == nil {
				t.Fatal("unsafe archive accepted")
			}
		})
	}
	if _, err := readArchive([]byte("not gzip"), true); err == nil {
		t.Fatal("bad gzip accepted")
	}
	if _, err := readArchive(archive(t, &tar.Header{Name: "package/unrelated", Typeflag: tar.TypeReg, Mode: 0755}), true); err == nil {
		t.Fatal("missing binary accepted")
	}
}

func TestUpdatePreservesActiveAndCompatibility(t *testing.T) {
	r, _ := fixture(t)
	if _, err := r.Install(context.Background(), "0.162.1", false); err != nil {
		t.Fatal(err)
	}
	active := filepath.Join(r.root, "active.json")
	b, err := os.ReadFile(active)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	m["allowed_versions"] = []string{"0.162.1"}
	b, _ = json.Marshal(m)
	if err := os.WriteFile(active, b, 0644); err != nil {
		t.Fatal(err)
	}
	r.rename = func(string, string) error { return errors.New("publication denied") }
	if _, err := r.Install(context.Background(), "0.162.1", true); err == nil {
		t.Fatal("publication failure accepted")
	}
	after, _ := os.ReadFile(active)
	if !bytes.Equal(b, after) {
		t.Fatal("failed update changed active metadata")
	}
	r.rename = os.Rename
	i, err := r.Install(context.Background(), "0.162.1", true)
	if err != nil || !reflect.DeepEqual(i.AllowedVersions, []string{"0.162.1"}) {
		t.Fatalf("compatibility lost: %+v %v", i, err)
	}
}

func TestUpdateRepairsCorruptedNative(t *testing.T) {
	r, _ := fixture(t)
	i, err := r.Install(context.Background(), "0.162.1", false)
	if err != nil {
		t.Fatal(err)
	}
	active := filepath.Join(r.root, "active.json")
	b, _ := os.ReadFile(active)
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	m["allowed_versions"] = []string{"0.162.1"}
	b, _ = json.Marshal(m)
	if err := os.WriteFile(active, b, 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(i.Path, []byte("corrupted fixture"), 0755); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Verify(); err == nil {
		t.Fatal("corruption accepted for use")
	}
	got, err := r.Install(context.Background(), "0.162.1", true)
	if err != nil || !reflect.DeepEqual(got.AllowedVersions, []string{"0.162.1"}) {
		t.Fatalf("repair failed: %+v %v", got, err)
	}
	if _, err := r.Verify(); err != nil {
		t.Fatal(err)
	}
}

func TestVerificationRejectsTampering(t *testing.T) {
	for _, name := range []string{"hash", "mode", "ancestor mode", "ownership", "symlink", "metadata mode", "metadata path"} {
		t.Run(name, func(t *testing.T) {
			r, _ := fixture(t)
			i, err := r.Install(context.Background(), "0.162.1", false)
			if err != nil {
				t.Fatal(err)
			}
			switch name {
			case "hash":
				err = os.WriteFile(i.Path, []byte("bad executable"), 0755)
			case "mode":
				err = os.Chmod(i.Path, 0777)
			case "ancestor mode":
				err = os.Chmod(filepath.Dir(i.Path), 0777)
			case "ownership":
				r.owned = func(os.FileInfo) bool { return false }
			case "symlink":
				err = os.Rename(i.Path, i.Path+".real")
				if err == nil {
					err = os.Symlink(i.Path+".real", i.Path)
				}
			case "metadata mode":
				err = os.Chmod(filepath.Join(r.root, "active.json"), 0666)
			case "metadata path":
				err = os.WriteFile(filepath.Join(r.root, "active.json"), []byte(`{"version":"0.162.1","path":"../escape","sha256":"bad","allowed_versions":[]}`), 0644)
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err := r.Verify(); err == nil {
				t.Fatal("tampering accepted")
			}
			r.execute = func(context.Context, command) ([]byte, error) {
				t.Fatal("executed invalid installation")
				return nil, nil
			}
			if err := r.Run(context.Background(), t.TempDir(), "login"); err == nil {
				t.Fatal("run accepted tampering")
			}
		})
	}
}

func TestRunSanitizesAndHidesChildFailure(t *testing.T) {
	r, _ := fixture(t)
	i, err := r.Install(context.Background(), "0.162.1", false)
	if err != nil {
		t.Fatal(err)
	}
	home := t.TempDir()
	if err := os.Chmod(home, 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("NODE_OPTIONS", "--require=evil")
	t.Setenv("npm_config_registry", "https://evil.example")
	t.Setenv("CODEX_HOME", "/personal/auth")
	t.Setenv("PATH", "/evil")
	r.execute = func(_ context.Context, c command) ([]byte, error) {
		if c.path != i.Path || c.dir != home || !reflect.DeepEqual(c.args, []string{"login"}) {
			t.Fatalf("bad process: %+v", c)
		}
		if !reflect.DeepEqual(c.env, []string{"HOME=" + home, "CODEX_HOME=" + home, "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=en_US.UTF-8"}) {
			t.Fatalf("bad environment: %v", c.env)
		}
		return []byte("secret-output"), errors.New("secret-error")
	}
	if err := r.Run(context.Background(), home, "login"); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("unsafe error: %v", err)
	}
}

func TestFixedLockAndCancellation(t *testing.T) {
	r, _ := fixture(t)
	if _, err := r.Install(context.Background(), "0.162.1", false); err != nil {
		t.Fatal(err)
	}
	unlock, err := r.Lock(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := r.Lock(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("lock cancellation: %v", err)
	}
	unlock()
	unlock()
	unlock2, err := r.Lock(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	unlock2()
	if err := os.Chmod(filepath.Join(r.root, "auth.lock"), 0666); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Lock(context.Background()); err == nil {
		t.Fatal("writable lock accepted")
	}
}
