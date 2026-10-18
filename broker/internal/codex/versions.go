package codex

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
)

var errReleaseMissing = errors.New("Codex registry release missing")

func Newer(a, b string) bool {
	if !ValidVersion(a) || !ValidVersion(b) {
		return false
	}
	x, y := strings.Split(a, "."), strings.Split(b, ".")
	for i := range x {
		if len(x[i]) != len(y[i]) {
			return len(x[i]) > len(y[i])
		}
		if x[i] != y[i] {
			return x[i] > y[i]
		}
	}
	return false
}

func (r Runtime) LatestAllowed(ctx context.Context, allowed []string) (string, error) {
	versions := slices.Clone(allowed)
	for _, v := range versions {
		if !ValidVersion(v) {
			return "", errors.New("invalid Codex compatibility rule")
		}
	}
	slices.SortFunc(versions, func(a, b string) int {
		if Newer(a, b) {
			return -1
		}
		if Newer(b, a) {
			return 1
		}
		return 0
	})
	for _, v := range slices.Compact(versions) {
		_, err := r.metadata(ctx, "@openai/codex", v)
		if errors.Is(err, errReleaseMissing) {
			continue
		}
		if err != nil {
			return "", err
		}
		return v, nil
	}
	return "", nil
}

func (r Runtime) Latest(ctx context.Context) (string, error) {
	b, err := r.download(ctx, registry+"/@openai%2Fcodex/latest", maxMetadata)
	if err != nil {
		return "", err
	}
	var m packageMetadata
	if json.Unmarshal(b, &m) != nil || m.Name != "@openai/codex" || !ValidVersion(m.Version) {
		return "", errors.New("invalid latest Codex release")
	}
	return m.Version, nil
}

func (r Runtime) Linux(ctx context.Context, version, arch string) ([]byte, error) {
	cpu, triple := "arm64", "aarch64-unknown-linux-musl"
	switch arch {
	case "arm64":
	case "amd64":
		cpu, triple = "x64", "x86_64-unknown-linux-musl"
	default:
		return nil, errors.New("unsupported Codex container architecture")
	}
	if !ValidVersion(version) {
		return nil, errors.New("Codex requires an exact stable version")
	}
	return r.platformEntry(ctx, version, "linux", cpu, triple)
}
