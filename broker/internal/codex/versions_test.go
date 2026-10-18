package codex

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestVersionOrdering(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"0.162.10", "0.162.9", true}, {"1.0.0", "0.999.0", true},
		{"0.163.0", "0.162.999", true}, {"0.162.1", "0.162.1", false},
		{"0.162.1-beta", "0.162.0", false}, {"01.1.1", "0.0.0", false},
		{"9999999999999999999999.0.0", "999999999999999999999.0.0", true},
	} {
		if got := Newer(c.a, c.b); got != c.want {
			t.Errorf("%s > %s: %v", c.a, c.b, got)
		}
	}
}

func registryFixture(entries map[string]string) Runtime {
	r := New()
	r.http = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		body, ok := entries[req.URL.Path]
		status := 200
		if !ok {
			status = 404
		}
		if body == "unavailable" {
			return nil, errors.New("offline")
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header), Request: req}, nil
	})}
	return r
}

func TestLatestAllowed(t *testing.T) {
	metadata := func(v string) string {
		return `{"name":"@openai/codex","version":"` + v + `","dist":{"tarball":"https://registry.npmjs.org/main.tgz"}}`
	}
	r := registryFixture(map[string]string{"/@openai/codex/0.162.9": metadata("0.162.9"), "/@openai/codex/0.162.10": metadata("0.162.10"), "/@openai/codex/0.164.0": "unavailable"})
	for _, c := range []struct {
		versions []string
		want     string
		fail     bool
	}{
		{[]string{"0.162.9", "0.162.10", "0.163.0"}, "0.162.10", false},
		{[]string{}, "", false}, {[]string{"0.163.0"}, "", false},
		{[]string{"0.164.0"}, "", true}, {[]string{"0.162.10-beta"}, "", true},
	} {
		got, err := r.LatestAllowed(context.Background(), c.versions)
		if got != c.want || (err != nil) != c.fail {
			t.Errorf("%v: %q %v", c.versions, got, err)
		}
	}
}

func TestLatestHint(t *testing.T) {
	for _, body := range []string{`{"name":"@openai/codex","version":"0.163.0"}`, `{"name":"other","version":"0.163.0"}`, `{"name":"@openai/codex","version":"0.163.0-beta"}`, "unavailable", `{`} {
		r := registryFixture(map[string]string{"/@openai/codex/latest": body})
		got, err := r.Latest(context.Background())
		good := body == `{"name":"@openai/codex","version":"0.163.0"}`
		if (err == nil) != good || (good && got != "0.163.0") {
			t.Errorf("%s: %q %v", body, got, err)
		}
	}
}

func TestLinuxArtifacts(t *testing.T) {
	for _, arch := range []string{"arm64", "amd64"} {
		for _, alias := range []bool{false, true} {
			t.Run(arch+map[bool]string{true: "-alias"}[alias], func(t *testing.T) {
				cpu, triple := "arm64", "aarch64-unknown-linux-musl"
				if arch == "amd64" {
					cpu, triple = "x64", "x86_64-unknown-linux-musl"
				}
				r, data := fixture(t)
				main := archive(t, &tar.Header{Name: "package/package.json", Typeflag: tar.TypeReg, Mode: 0644, Size: 1})
				native := archive(t, &tar.Header{Name: "package/vendor/" + triple + "/bin/codex", Typeflag: tar.TypeReg, Mode: 0755, Size: 6})
				metadata := func(name, v, tarball string, body []byte, extra map[string]any) []byte {
					sum := sha512.Sum512(body)
					m := map[string]any{"name": name, "version": v, "dist": map[string]string{"tarball": tarball, "integrity": "sha512-" + base64.StdEncoding.EncodeToString(sum[:])}}
					for k, v := range extra {
						m[k] = v
					}
					b, _ := json.Marshal(m)
					return b
				}
				name, version := "@openai/codex-linux-"+cpu, "0.162.1"
				dep := version
				if alias {
					name, version = "@openai/codex", version+"-linux-"+cpu
					dep = "npm:" + name + "@" + version
				}
				data["https://registry.npmjs.org/@openai%2Fcodex/0.162.1"] = metadata("@openai/codex", "0.162.1", "https://registry.npmjs.org/main.tgz", main, map[string]any{"optionalDependencies": map[string]string{"@openai/codex-linux-" + cpu: dep}})
				platformURL := "https://registry.npmjs.org/" + strings.ReplaceAll(name, "/", "%2F") + "/" + version
				data[platformURL] = metadata(name, version, "https://registry.npmjs.org/native.tgz", native, map[string]any{"os": []string{"linux"}, "cpu": []string{cpu}})
				data["https://registry.npmjs.org/main.tgz"], data["https://registry.npmjs.org/native.tgz"] = main, native
				got, err := r.Linux(context.Background(), "0.162.1", arch)
				if err != nil || !bytes.Equal(got, []byte("xxxxxx")) {
					t.Fatalf("%q %v", got, err)
				}
				data["https://registry.npmjs.org/native.tgz"] = []byte("corrupt")
				if _, err := r.Linux(context.Background(), "0.162.1", arch); err == nil {
					t.Fatal("corrupt accepted")
				}
				data[platformURL] = metadata(name, version, "https://registry.npmjs.org/native.tgz", native, map[string]any{"os": []string{"darwin"}, "cpu": []string{cpu}})
				if _, err := r.Linux(context.Background(), "0.162.1", arch); err == nil {
					t.Fatal("wrong target accepted")
				}
			})
		}
	}
	for _, c := range [][2]string{{"0.162.1", "ppc64"}, {"latest", "arm64"}} {
		if _, err := New().Linux(context.Background(), c[0], c[1]); err == nil {
			t.Fatal("invalid target accepted")
		}
	}
}
