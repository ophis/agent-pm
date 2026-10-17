package launcher

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type Manifest struct {
	Name, Generation, Image, CWD, ClaudeDir, HookPath string
	HasHook                                           bool
	Labels                                            map[string]string
}

var (
	namePattern       = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	generationPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
	imagePattern      = regexp.MustCompile(`^agent-pm-worker:[0-9a-f]{64}$`)
	labelKeyPattern   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
)

func hasControl(s string) bool {
	return strings.ContainsFunc(s, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

// strictJSON rejects duplicate keys and anything after the first value; Token rejects NaN and other non-JSON.
func strictJSON(data []byte) error {
	type frame struct {
		object, wantKey bool
		keys            map[string]bool
	}
	var stack []*frame
	done := func() {
		if n := len(stack); n > 0 && stack[n-1].object {
			stack[n-1].wantKey = true
		}
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	values := 0
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if len(stack) == 0 {
			if values++; values > 1 {
				return errors.New("trailing data after the manifest")
			}
		}
		if n := len(stack); n > 0 && stack[n-1].object && stack[n-1].wantKey {
			if key, ok := tok.(string); ok {
				if stack[n-1].keys[key] {
					return fmt.Errorf("duplicate key %q", key)
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
		done()
	}
	if values == 0 {
		return errors.New("empty manifest")
	}
	return nil
}

func jsonString(raw json.RawMessage) (string, bool) {
	var s string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &s) != nil || hasControl(s) {
		return "", false
	}
	return s, true
}

// ParseManifest validates a manifest against protocol 1's schema; every failure is manifest_rejected.
func ParseManifest(data []byte) (Manifest, error) {
	m, err := parseManifest(data)
	if err != nil {
		e := protocol.New(protocol.ManifestRejected, "Manifest rejected: "+err.Error())
		return Manifest{}, e
	}
	return m, nil
}

func parseManifest(data []byte) (Manifest, error) {
	if !utf8.Valid(data) {
		return Manifest{}, errors.New("not UTF-8")
	}
	if err := strictJSON(data); err != nil {
		return Manifest{}, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields == nil {
		return Manifest{}, errors.New("not a JSON object")
	}
	var m Manifest
	targets := map[string]*string{"name": &m.Name, "generation": &m.Generation, "image": &m.Image,
		"cwd": &m.CWD, "claude_dir": &m.ClaudeDir, "hook_path": &m.HookPath}
	for key, raw := range fields {
		if key == "labels" {
			continue
		}
		target, ok := targets[key]
		if !ok {
			return Manifest{}, fmt.Errorf("unknown field %q", key)
		}
		if *target, ok = jsonString(raw); !ok {
			return Manifest{}, fmt.Errorf("field %q must be a string without control characters", key)
		}
	}
	for _, key := range []string{"name", "generation", "image", "cwd", "claude_dir", "labels"} {
		if _, ok := fields[key]; !ok {
			return Manifest{}, fmt.Errorf("missing field %q", key)
		}
	}
	_, m.HasHook = fields["hook_path"]
	switch {
	case !namePattern.MatchString(m.Name) || m.Name == "none":
		return Manifest{}, errors.New("invalid name")
	case !generationPattern.MatchString(m.Generation):
		return Manifest{}, errors.New("invalid generation")
	case !imagePattern.MatchString(m.Image):
		return Manifest{}, errors.New("image must be agent-pm-worker:<64 hex>")
	case !strings.HasPrefix(m.CWD, "/") || !strings.HasPrefix(m.ClaudeDir, "/") || m.HasHook && !strings.HasPrefix(m.HookPath, "/"):
		return Manifest{}, errors.New("paths must be absolute")
	}
	m.CWD = filepath.Clean(m.CWD) // Docker mounts at the cleaned target

	var labels map[string]json.RawMessage
	if raw := fields["labels"]; len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &labels) != nil {
		return Manifest{}, errors.New("labels must be an object")
	}
	m.Labels = map[string]string{}
	for key, raw := range labels {
		value, ok := jsonString(raw)
		if !ok || !labelKeyPattern.MatchString(key) || strings.HasPrefix(key, "agent-pm.") {
			return Manifest{}, fmt.Errorf("invalid label %q", key)
		}
		m.Labels[key] = value
	}
	return m, nil
}
