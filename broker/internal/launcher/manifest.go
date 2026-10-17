package launcher

import (
	"bytes"
	"encoding/json"
	"io"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type Manifest struct {
	Name, Generation, Image, CWD, Workspace, ClaudeDir, HookPath string
	HasHook                                                      bool
	Labels                                                       map[string]string
	GitHub                                                       []GitHubToken
	ContainerConfig                                              string // TOML the container reads as its local agent-pm config
}

// GitHubToken is one installation token a start requests: an App's, for one repo, with these permissions.
type GitHubToken struct {
	Repo, App   string
	AppID       int64
	Permissions map[string]string
}

// A workspace volume's labels: its Kind and its mount point.
const (
	kindLabel      = "agent-pm.kind"
	workspaceLabel = "agent-pm.workspace"
)

// Kind is the workspace volume's kindLabel: run or crew.
func (m Manifest) Kind() string {
	if strings.HasPrefix(m.Workspace, "agent-pm-run-") {
		return "run"
	}
	return "crew"
}

var (
	namePattern       = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	generationPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	imagePattern      = regexp.MustCompile(`^agent-pm-worker:[0-9a-f]{64}$`)
	labelKeyPattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	runVolumePattern  = regexp.MustCompile(`^agent-pm-run-[A-Za-z0-9_.-]+-[0-9a-f]{8}$`)
	repoPattern       = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}$`)
	appPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	appIDPattern      = regexp.MustCompile(`^[1-9][0-9]{0,15}$`)
	permissionPattern = regexp.MustCompile(`^[a-z][a-z_]{0,62}$`)
)

const (
	maxContainerConfig = 65536
	maxAppID           = 1<<53 - 1 // the schema's integer bound
)

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
		"cwd": &m.CWD, "workspace": &m.Workspace, "claude_dir": &m.ClaudeDir, "hook_path": &m.HookPath}
	for key, raw := range fields {
		if key == "labels" || key == "github" || key == "container_config" {
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
	for _, key := range []string{"name", "generation", "image", "cwd", "workspace", "claude_dir", "labels", "github", "container_config"} {
		if _, ok := fields[key]; !ok {
			return reject("missing field %q", key)
		}
	}
	_, m.HasHook = fields["hook_path"]
	switch {
	case !namePattern.MatchString(m.Name) || m.Name == "none":
		return reject("invalid name")
	case !generationPattern.MatchString(m.Generation):
		return reject("invalid generation")
	case !imagePattern.MatchString(m.Image):
		return reject("image must be agent-pm-worker:<64 hex>")
	case m.Workspace != "agent-pm-crew-"+m.Name && !runVolumePattern.MatchString(m.Workspace):
		return reject("workspace must be agent-pm-crew-<name> or agent-pm-run-<dir>-<8 hex>")
	case !strings.HasPrefix(m.CWD, "/") || !strings.HasPrefix(m.ClaudeDir, "/") || m.HasHook && !strings.HasPrefix(m.HookPath, "/"):
		return reject("paths must be absolute")
	}
	m.CWD = filepath.Clean(m.CWD) // Docker mounts at the cleaned target

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
	var config string
	if raw := fields["container_config"]; len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &config) != nil ||
		len(config) > maxContainerConfig || strings.ContainsFunc(config, func(r rune) bool { return (r < 0x20 && r != '\n' && r != '\t') || r == 0x7f }) {
		return reject("container_config must be a string of at most %d bytes without control characters but newline and tab", maxContainerConfig)
	}
	m.ContainerConfig = config
	tokens, err := parseGitHub(fields["github"])
	if err != nil {
		return reject("%v", err)
	}
	m.GitHub = tokens
	return m, nil
}

type manifestError string

func (e manifestError) Error() string { return string(e) }

// parseGitHub validates the github token list; the tokenizer above already refused duplicate keys.
func parseGitHub(raw json.RawMessage) ([]GitHubToken, error) {
	var items []map[string]json.RawMessage
	if len(raw) == 0 || raw[0] != '[' || json.Unmarshal(raw, &items) != nil {
		return nil, manifestError("github must be an array of objects")
	}
	tokens := make([]GitHubToken, 0, len(items))
	for i, item := range items {
		bad := func(what string) error { return manifestError("github[" + strconv.Itoa(i) + "]: " + what) }
		if item == nil || len(item) != 4 {
			return nil, bad("want exactly repo, app, app_id and permissions")
		}
		var token GitHubToken
		var permissions map[string]string
		repo, app, id := item["repo"], item["app"], string(item["app_id"])
		switch {
		case json.Unmarshal(repo, &token.Repo) != nil || !repoPattern.MatchString(token.Repo) || strings.HasSuffix(token.Repo, "/.") || strings.HasSuffix(token.Repo, "/.."):
			return nil, bad("repo must be owner/name")
		case json.Unmarshal(app, &token.App) != nil || !appPattern.MatchString(token.App):
			return nil, bad("invalid app")
		case !appIDPattern.MatchString(id):
			return nil, bad("app_id must be a positive integer")
		case json.Unmarshal(item["permissions"], &permissions) != nil || len(permissions) == 0:
			return nil, bad("permissions must be a nonempty object")
		}
		if token.AppID, _ = strconv.ParseInt(id, 10, 64); token.AppID > maxAppID {
			return nil, bad("app_id must be a positive integer")
		}
		for name, level := range permissions {
			if !permissionPattern.MatchString(name) || level != "read" && level != "write" {
				return nil, bad("permission " + strconv.Quote(name) + " must be read or write")
			}
		}
		token.Permissions = permissions
		tokens = append(tokens, token)
	}
	return tokens, nil
}
