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

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

//go:embed assets/*
var assets embed.FS

var digestPattern = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var versionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$`)

type imageInfo struct {
	ID     string `json:"Id"`
	Config struct {
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
func inspectImage(ctx context.Context, d docker.Runner, image string) (imageInfo, bool, error) {
	out, err := d.Run(ctx, []string{"image", "inspect", image}, nil)
	if absent(err) {
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

func probeIDs(out []byte) (string, string, bool) {
	fields := strings.Fields(string(out))
	if len(fields) != 2 {
		return "", "", false
	}
	for _, f := range fields {
		if n, err := strconv.ParseUint(f, 10, 31); err != nil || n == 0 {
			return "", "", false
		}
	}
	return fields[0], fields[1], true
}

// Build produces agent-pm-worker:<key> in two stages; an image with this key and version is reused.
func (h Host) Build(ctx context.Context, pkg string) (BuildResult, error) {
	failed := func(message string, err error) (BuildResult, error) {
		e := protocol.New(protocol.BuildFailed, message)
		if err != nil {
			e.Cause = err.Error()
		}
		return BuildResult{}, e
	}
	tmp, err := os.MkdirTemp("", "agent-pm-build-")
	if err != nil {
		return failed("Cannot create a build directory.", err)
	}
	defer os.RemoveAll(tmp)
	userDir, packageDir := filepath.Join(tmp, "user"), filepath.Join(tmp, "package")
	definition := filepath.Join(h.Home, ".agent-pm/docker/worker")
	if info, err := os.Stat(definition); err != nil || !info.IsDir() {
		return failed("No worker image definition at "+definition+"; run sh <agent-pm plugin directory>/broker/install.sh.", nil)
	}
	if err := snapshotTree(definition, userDir); err != nil {
		return failed("Cannot read the worker image definition.", err)
	}
	for _, rel := range []string{"session/src", "utils/src", ".claude-plugin/plugin.json"} {
		if err := snapshotTree(filepath.Join(pkg, rel), filepath.Join(packageDir, rel)); err != nil {
			return failed("Invalid agent-pm package directory: "+rel+" unreadable.", err)
		}
	}
	var meta struct {
		Version string `json:"version"`
	}
	data, err := os.ReadFile(filepath.Join(packageDir, ".claude-plugin/plugin.json"))
	if err != nil || json.Unmarshal(data, &meta) != nil || !versionPattern.MatchString(meta.Version) {
		return failed("Invalid agent-pm package version in .claude-plugin/plugin.json.", nil)
	}
	entries, err := assets.ReadDir("assets")
	if err != nil {
		return failed("Embedded image assets unreadable.", err)
	}
	for _, entry := range entries {
		data, err := assets.ReadFile("assets/" + entry.Name())
		if err == nil {
			err = os.WriteFile(filepath.Join(packageDir, entry.Name()), data, 0o644)
		}
		if err != nil {
			return failed("Cannot stage image assets.", err)
		}
	}
	key, err := treeDigest(tmp)
	if err != nil {
		return failed("Cannot hash the build context.", err)
	}
	image, userImage := "agent-pm-worker:"+key, "agent-pm-user:"+key
	info, found, err := inspectImage(ctx, h.Docker, image)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Cannot inspect the worker image.")
	}
	if found && info.Config.Labels["agent-pm.key"] == key && info.Config.Labels["agent-pm.version"] == meta.Version {
		return BuildResult{Image: image, Digest: info.ID}, nil
	}
	if _, err := h.Docker.Run(ctx, []string{"build", "--progress=plain", "--tag", userImage, userDir}, nil); err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Building the worker image definition failed.")
	}
	out, err := h.Docker.Run(ctx, []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--entrypoint", "/bin/sh", userImage, "-c", "id -u agent && id -g agent"}, nil)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "The worker image has no usable user agent.")
	}
	uid, gid, ok := probeIDs(out)
	if !ok {
		return failed("The worker image's user agent must be a non-root user.", nil)
	}
	args := []string{"build", "--progress=plain", "--build-arg", "USER_IMAGE=" + userImage, "--build-arg", "AGENT_PM_VERSION=" + meta.Version,
		"--build-arg", "AGENT_UID=" + uid, "--build-arg", "AGENT_GID=" + gid, "--label", "agent-pm.key=" + key, "--tag", image, packageDir}
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Building the agent-pm image stage failed.")
	}
	info, found, err = inspectImage(ctx, h.Docker, image)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Cannot inspect the built image.")
	}
	labels := info.Config.Labels
	if !found || labels["agent-pm.key"] != key || labels["agent-pm.version"] != meta.Version || labels["agent-pm.agent-uid"] != uid || labels["agent-pm.agent-gid"] != gid {
		return failed("Built image labels do not match the package.", nil)
	}
	return BuildResult{Image: image, Digest: info.ID, Rebuilt: true}, nil
}
