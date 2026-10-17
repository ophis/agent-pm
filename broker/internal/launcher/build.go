package launcher

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

//go:embed assets/*
var assets embed.FS

var digestPattern = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var versionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$`)
var builtPattern = regexp.MustCompile(`^agent-pm-(?:user|worker):[0-9a-f]{64}$`)

type imageInfo struct {
	ID      string `json:"Id"`
	Created time.Time
	Config  struct {
		Labels map[string]string
	}
}

func snapshotTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.Name() == "__pycache__" {
			return filepath.SkipDir
		}
		if strings.HasSuffix(entry.Name(), ".pyc") {
			return nil
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case info.IsDir():
			return os.MkdirAll(target, info.Mode().Perm())
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
				return err
			}
			return os.Symlink(link, target)
		case info.Mode().IsRegular():
			in, err := os.Open(path)
			if err != nil {
				return err
			}
			defer in.Close()
			if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return err
			}
			out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode().Perm())
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(out, in)
			closeErr := out.Close()
			return errors.Join(copyErr, closeErr)
		default:
			return fmt.Errorf("unsupported build context file: %s", path)
		}
	})
}

func treeDigest(root string) (string, error) {
	h := sha256.New()
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		fmt.Fprintf(h, "%d:%s:%o:", len(rel), rel, info.Mode())
		switch {
		case info.Mode().IsRegular():
			file, err := os.Open(path)
			if err != nil {
				return err
			}
			defer file.Close()
			fmt.Fprintf(h, "%d:", info.Size())
			_, err = io.Copy(h, file)
			return err
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			fmt.Fprintf(h, "%d:%s", len(link), link)
			return err
		}
		return nil
	})
	return hex.EncodeToString(h.Sum(nil)), err
}

// inspectImage returns found=false when Docker reports the image absent.
func (h Host) inspectImage(ctx context.Context, image string) (imageInfo, bool, error) {
	out, err := h.Docker.Run(ctx, []string{"image", "inspect", image}, nil)
	if docker.Absent(err) {
		return imageInfo{}, false, nil
	}
	if err != nil {
		return imageInfo{}, false, err
	}
	var infos []imageInfo
	if err := json.Unmarshal(out, &infos); err != nil || len(infos) != 1 || !digestPattern.MatchString(infos[0].ID) {
		return imageInfo{}, false, errors.New("invalid image inspection")
	}
	return infos[0], true, nil
}

func numeric(s string) bool {
	n, err := strconv.ParseUint(s, 10, 31)
	return err == nil && n > 0
}

// checkImage requires a broker-built image: matching key, version and agent ids in its labels.
func (h Host) checkImage(ctx context.Context, image string) (imageInfo, error) {
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return imageInfo{}, failure(err, protocol.ImageRejected, "Cannot inspect image "+image+".")
	}
	l := info.Config.Labels
	if !found || l["agent-pm.key"] != strings.TrimPrefix(image, "agent-pm-worker:") || l["agent-pm.version"] == "" ||
		!numeric(l["agent-pm.agent-uid"]) || !numeric(l["agent-pm.agent-gid"]) {
		return imageInfo{}, protocol.New(protocol.ImageRejected, "Image "+image+" is not a broker-built worker image; run agent-pm-broker build.")
	}
	return info, nil
}

// stage snapshots the worker image definition, the package and the embedded assets into tmp; it returns the package version.
func (h Host) stage(pkg, tmp string) (string, error) {
	userDir, packageDir := filepath.Join(tmp, "user"), filepath.Join(tmp, "package")
	definition := filepath.Join(h.Home, ".agent-pm/docker/worker")
	if info, err := os.Stat(definition); err != nil || !info.IsDir() {
		return "", protocol.New(protocol.BuildFailed, "No worker image definition at "+definition+"; run sh <agent-pm plugin directory>/broker/install.sh.")
	}
	if err := snapshotTree(definition, userDir); err != nil {
		return "", protocol.New(protocol.BuildFailed, "Cannot read the worker image definition.")
	}
	for _, rel := range []string{"session/src", "utils/src", ".claude-plugin/plugin.json"} {
		if err := snapshotTree(filepath.Join(pkg, rel), filepath.Join(packageDir, rel)); err != nil {
			return "", protocol.New(protocol.BuildFailed, "Invalid agent-pm package directory: "+rel+" unreadable.")
		}
	}
	var meta struct {
		Version string `json:"version"`
	}
	data, err := os.ReadFile(filepath.Join(packageDir, ".claude-plugin/plugin.json"))
	if err != nil || json.Unmarshal(data, &meta) != nil || !versionPattern.MatchString(meta.Version) {
		return "", protocol.New(protocol.BuildFailed, "Invalid agent-pm package version in .claude-plugin/plugin.json.")
	}
	entries, err := assets.ReadDir("assets")
	if err != nil {
		return "", protocol.New(protocol.BuildFailed, "Embedded image assets unreadable.")
	}
	for _, entry := range entries {
		data, err := assets.ReadFile("assets/" + entry.Name())
		if err == nil {
			err = os.WriteFile(filepath.Join(packageDir, entry.Name()), data, 0o644)
		}
		if err != nil {
			return "", protocol.New(protocol.BuildFailed, "Cannot stage image assets.")
		}
	}
	return meta.Version, nil
}

// Build produces agent-pm-worker:<key> in two stages; an image with this key and version built less than maxAge ago
// is reused (maxAge < 0: of any age; 0: never). A rebuild builds the user stage without cache, so it installs the latest clients.
func (h Host) Build(ctx context.Context, pkg string, maxAge time.Duration) (BuildResult, error) {
	tmp, err := docker.TempDir(h.Home, "agent-pm-build-")
	if err != nil {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Cannot create a build directory.")
	}
	defer os.RemoveAll(tmp)
	version, err := h.stage(pkg, tmp)
	if err != nil {
		return BuildResult{}, err
	}
	key, err := treeDigest(tmp)
	if err != nil {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Cannot hash the build context.")
	}
	image := "agent-pm-worker:" + key
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Cannot inspect the worker image.")
	}
	current := found && info.Config.Labels["agent-pm.key"] == key && info.Config.Labels["agent-pm.version"] == version
	if current && (maxAge < 0 || (maxAge > 0 && time.Since(info.Created) < maxAge)) {
		return BuildResult{Image: image, Digest: info.ID}, nil
	}
	result, err := h.rebuild(ctx, tmp, key, version)
	var e *protocol.Error
	if current && maxAge > 0 && errors.As(err, &e) && e.Code == protocol.BuildFailed {
		// An age-only refresh that fails (e.g. offline) keeps the usable image.
		why := e.Message
		if e.Cause != "" {
			why += " (" + e.Cause + ")"
		}
		h.note("kept " + image + ": refreshing it failed: " + why)
		return BuildResult{Image: image, Digest: info.ID}, nil
	}
	return result, err
}

// rebuild builds both stages of key's images from the context in tmp.
func (h Host) rebuild(ctx context.Context, tmp, key, version string) (BuildResult, error) {
	image, userImage := "agent-pm-worker:"+key, "agent-pm-user:"+key
	if _, err := h.Docker.Run(ctx, []string{"build", "--progress=plain", "--no-cache", "--label", "agent-pm.key=" + key, "--tag", userImage, filepath.Join(tmp, "user")}, nil); err != nil {
		return BuildResult{}, diagnosed(err, protocol.BuildFailed, "Building the worker image definition failed.")
	}
	out, err := h.Docker.Run(ctx, []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--entrypoint", "/bin/sh", userImage, "-c", "id -u agent && id -g agent"}, nil)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "The worker image has no usable user agent.")
	}
	ids := strings.Fields(string(out))
	if len(ids) != 2 || !numeric(ids[0]) || !numeric(ids[1]) {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "The worker image's user agent must be a non-root user.")
	}
	uid, gid := ids[0], ids[1]
	args := []string{"build", "--progress=plain", "--build-arg", "USER_IMAGE=" + userImage, "--build-arg", "AGENT_PM_VERSION=" + version,
		"--build-arg", "AGENT_UID=" + uid, "--build-arg", "AGENT_GID=" + gid, "--label", "agent-pm.key=" + key, "--tag", image, filepath.Join(tmp, "package")}
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		return BuildResult{}, diagnosed(err, protocol.BuildFailed, "Building the agent-pm image stage failed.")
	}
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Cannot inspect the built image.")
	}
	labels := info.Config.Labels
	if !found || labels["agent-pm.key"] != key || labels["agent-pm.version"] != version || labels["agent-pm.agent-uid"] != uid || labels["agent-pm.agent-gid"] != gid {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Built image labels do not match the package.")
	}
	h.prune(ctx, key)
	return BuildResult{Image: image, Digest: info.ID, Rebuilt: true}, nil
}

// prune removes the user and worker images of keys other than key, and the untagged ones a same-key rebuild left,
// that no container, running or stopped, uses, noting each to Progress; whatever fails is kept and never fails the build.
func (h Host) prune(ctx context.Context, key string) {
	used, err := h.usedImages(ctx)
	if err != nil {
		h.note("kept older images: cannot list containers: " + docker.Diagnostic(err))
		return
	}
	out, err := h.Docker.Run(ctx, []string{"image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"}, nil)
	if err != nil {
		h.note("kept older images: cannot list images: " + docker.Diagnostic(err))
		return
	}
	for _, line := range strings.Split(string(out), "\n") {
		ref, id, _ := strings.Cut(line, " ")
		if !builtPattern.MatchString(ref) || strings.HasSuffix(ref, ":"+key) || used[id] {
			continue
		}
		// No --force: Docker also refuses an image a container uses.
		if _, err := h.Docker.Run(ctx, []string{"image", "rm", ref}, nil); err != nil {
			h.note("kept " + ref + ": " + docker.Diagnostic(err))
		} else {
			h.note("removed " + ref)
		}
	}
	out, err = h.Docker.Run(ctx, []string{"image", "ls", "--quiet", "--no-trunc", "--filter", "dangling=true", "--filter", "label=agent-pm.key"}, nil)
	if err != nil {
		h.note("kept untagged images: cannot list them: " + docker.Diagnostic(err))
		return
	}
	for _, id := range strings.Fields(string(out)) {
		if used[id] {
			continue
		}
		if _, err := h.Docker.Run(ctx, []string{"image", "rm", id}, nil); err != nil {
			h.note("kept " + id + ": " + docker.Diagnostic(err))
		} else {
			h.note("removed " + id)
		}
	}
}

func (h Host) note(s string) {
	if h.Progress != nil {
		fmt.Fprintln(h.Progress, "agent-pm-broker: "+s)
	}
}

// usedImages is the set of image IDs every container, running or stopped, was created from.
func (h Host) usedImages(ctx context.Context) (map[string]bool, error) {
	out, err := h.Docker.Run(ctx, []string{"container", "ls", "--all", "--quiet", "--no-trunc"}, nil)
	ids := strings.Fields(string(out))
	if err != nil || len(ids) == 0 {
		return nil, err
	}
	out, err = h.Docker.Run(ctx, append([]string{"container", "inspect", "--format", "{{.Image}}"}, ids...), nil)
	used := map[string]bool{}
	for _, id := range strings.Fields(string(out)) {
		used[id] = true
	}
	return used, err
}
