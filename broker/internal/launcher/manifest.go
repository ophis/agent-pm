package launcher

import (
	"bytes"
	"encoding/json"
	"io"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type Manifest struct {
	Name, Generation, Image, CWD, Workspace, Client, ClaudeDir, CodexDir, OpencodeDir, HookPath string
	HasHook                                                                                     bool
	Labels                                                                                      map[string]string
	GitHubApp                                                                                   string // the App whose tokens the session gets; "": github null
}

// A workspace volume's labels: its kind (always kind) and its mount point.
const (
	kindLabel      = "agent-pm.kind"
	kind           = "ws"
	workspaceLabel = "agent-pm.workspace"
)

var (
	namePattern       = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	generationPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	imagePattern      = regexp.MustCompile(`^agent-pm-worker:[0-9a-f]{64}$`)
	labelKeyPattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	appPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	// An OpenCode provider's name: its keychain account and its key file's name, <name>.key.
	providerPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
)

// ValidProvider is whether name can be a listed OpenCode provider; ollama needs no key.
func ValidProvider(name string) bool { return providerPattern.MatchString(name) && name != "ollama" }

func hasControl(s string) bool {
	return strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

// ParseManifest validates a manifest against the protocol's schema; every failure is manifest_rejected.
func ParseManifest(data []byte) (Manifest, error) {
	reject := func(format string, args ...any) (Manifest, error) {
		return Manifest{}, protocol.Newf(protocol.ManifestRejected, "Manifest rejected: "+format, args...)
	}
	if !utf8.Valid(data) {
		return reject("not UTF-8")
	}
	// Reject duplicate keys and anything after the first value; Token rejects NaN and other non-JSON.
	type frame struct {
		object, wantKey bool
		keys            map[string]bool
	}
	var stack []*frame
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	values := 0
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return reject("%v", err)
		}
		if len(stack) == 0 {
			if values++; values > 1 {
				return reject("trailing data after the manifest")
			}
		}
		if n := len(stack); n > 0 && stack[n-1].object && stack[n-1].wantKey {
			if key, ok := tok.(string); ok {
				if stack[n-1].keys[key] {
					return reject("duplicate key %q", key)
				}
				stack[n-1].keys[key], stack[n-1].wantKey = true, false
				continue
			}
		}
		switch tok {
		case json.Delim('{'):
			stack = append(stack, &frame{object: true, wantKey: true, keys: map[string]bool{}})
			continue
		case json.Delim('['):
			stack = append(stack, &frame{})
			continue
		case json.Delim('}'), json.Delim(']'):
			stack = stack[:len(stack)-1]
		}
		if n := len(stack); n > 0 && stack[n-1].object {
			stack[n-1].wantKey = true
		}
	}
	if values == 0 {
		return reject("empty manifest")
	}

	str := func(raw json.RawMessage) (string, bool) {
		var s string
		if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &s) != nil || hasControl(s) {
			return "", false
		}
		return s, true
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields == nil {
		return reject("not a JSON object")
	}
	var m Manifest
	targets := map[string]*string{"name": &m.Name, "generation": &m.Generation, "image": &m.Image,
		"cwd": &m.CWD, "workspace": &m.Workspace, "client": &m.Client, "claude_dir": &m.ClaudeDir, "codex_dir": &m.CodexDir,
		"opencode_dir": &m.OpencodeDir, "hook_path": &m.HookPath}
	for key, raw := range fields {
		if key == "labels" || key == "github" {
			continue
		}
		target, ok := targets[key]
		if !ok {
			return reject("unknown field %q", key)
		}
		if *target, ok = str(raw); !ok {
			return reject("field %q must be a string without control characters", key)
		}
	}
	for _, key := range []string{"name", "generation", "image", "cwd", "workspace", "client", "github", "labels"} {
		if _, ok := fields[key]; !ok {
			return reject("missing field %q", key)
		}
	}
	_, m.HasHook = fields["hook_path"]
	own := false
	for _, client := range []string{"claude", "codex", "opencode"} {
		_, present := fields[client+"_dir"]
		if present != (m.Client == client) {
			return reject("client requires exactly its own state directory")
		}
		own = own || present
	}
	if !own {
		return reject("client requires exactly its own state directory")
	}
	switch {
	case !namePattern.MatchString(m.Name) || m.Name == "none":
		return reject("invalid name")
	case !generationPattern.MatchString(m.Generation):
		return reject("invalid generation")
	case !imagePattern.MatchString(m.Image):
		return reject("image must be agent-pm-worker:<64 hex>")
	case m.Workspace != "agent-pm-ws-"+m.Name:
		return reject("workspace must be agent-pm-ws-<name>")
	case !strings.HasPrefix(m.CWD, "/") || !strings.HasPrefix(m.StateDir(), "/") || m.HasHook && !strings.HasPrefix(m.HookPath, "/"):
		return reject("paths must be absolute")
	}
	m.CWD = filepath.Clean(m.CWD) // Docker mounts at the cleaned target
	if raw := fields["github"]; string(raw) != "null" {
		var github map[string]json.RawMessage
		if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &github) != nil || len(github) != 1 {
			return reject("github must be null or {\"app\": <name>}")
		}
		app, ok := str(github["app"])
		if !ok || !appPattern.MatchString(app) {
			return reject("github must be null or {\"app\": <name>}")
		}
		m.GitHubApp = app
	}

	var labels map[string]json.RawMessage
	if raw := fields["labels"]; len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &labels) != nil {
		return reject("labels must be an object")
	}
	m.Labels = map[string]string{}
	for key, raw := range labels {
		value, ok := str(raw)
		if !ok || !labelKeyPattern.MatchString(key) || strings.HasPrefix(key, "agent-pm.") {
			return reject("invalid label %q", key)
		}
		m.Labels[key] = value
	}
	return m, nil
}

func (m Manifest) StateDir() string {
	switch m.Client {
	case "codex":
		return m.CodexDir
	case "opencode":
		return m.OpencodeDir
	}
	return m.ClaudeDir
}
