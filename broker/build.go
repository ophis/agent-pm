package main

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
	"strings"
)

//go:embed assets/*
var assets embed.FS

var imagePattern = regexp.MustCompile(`^agent-pm-worker:[a-f0-9]{64}$`)
var digestPattern = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var versionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$`)

type BuildResult struct {
	Image   string `json:"image"`
	Digest  string `json:"digest"`
	Rebuilt bool   `json:"rebuilt"`
}

type imageConfig struct {
	Labels  map[string]string
	Volumes map[string]json.RawMessage
}
type imageInfo struct {
	ID     string `json:"Id"`
	Config imageConfig
}
type receipt struct {
	Image   string `json:"image"`
	Digest  string `json:"digest"`
	Version string `json:"version"`
}
type builder struct {
	home   string
	docker runner
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

func inspectImage(ctx context.Context, d runner, image string) (imageInfo, error) {
	out, err := d.Run(ctx, []string{"image", "inspect", image}, nil)
	if err != nil {
		return imageInfo{}, err
	}
	var infos []imageInfo
	if err := json.Unmarshal(out, &infos); err != nil {
		return imageInfo{}, fmt.Errorf("invalid image inspection: %w", err)
	}
	if len(infos) != 1 || !digestPattern.MatchString(infos[0].ID) {
		return imageInfo{}, errors.New("invalid image digest")
	}
	return infos[0], nil
}

func receiptPath(home, image string) string {
	return filepath.Join(home, ".agent-pm/docker-state/images", strings.TrimPrefix(image, "agent-pm-worker:")+".json")
}

func (b builder) Build(ctx context.Context, pkg string) (BuildResult, error) {
	if !filepath.IsAbs(pkg) {
		return BuildResult{}, errors.New("--agent-pm requires an absolute package directory")
	}
	tmp, err := os.MkdirTemp("", "agent-pm-build-")
	if err != nil {
		return BuildResult{}, err
	}
	defer os.RemoveAll(tmp)
	userDir, packageDir := filepath.Join(tmp, "user"), filepath.Join(tmp, "package")
	if err := snapshotTree(filepath.Join(b.home, ".agent-pm/docker/worker"), userDir); err != nil {
		return BuildResult{}, fmt.Errorf("worker definition unavailable; run installer: %w", err)
	}
	for _, rel := range []string{"session/src", "utils/src", ".claude-plugin/plugin.json"} {
		if err := snapshotTree(filepath.Join(pkg, rel), filepath.Join(packageDir, rel)); err != nil {
			return BuildResult{}, err
		}
	}
	var meta struct {
		Version string `json:"version"`
	}
	data, err := os.ReadFile(filepath.Join(packageDir, ".claude-plugin/plugin.json"))
	if err != nil {
		return BuildResult{}, err
	}
	if err := json.Unmarshal(data, &meta); err != nil || !versionPattern.MatchString(meta.Version) {
		return BuildResult{}, errors.New("invalid package version")
	}
	entries, err := assets.ReadDir("assets")
	if err != nil {
		return BuildResult{}, err
	}
	for _, entry := range entries {
		data, err := assets.ReadFile("assets/" + entry.Name())
		if err != nil {
			return BuildResult{}, err
		}
		if err := os.WriteFile(filepath.Join(packageDir, entry.Name()), data, 0644); err != nil {
			return BuildResult{}, err
		}
	}
	key, err := treeDigest(tmp)
	if err != nil {
		return BuildResult{}, err
	}
	image := "agent-pm-worker:" + key
	if data, err := os.ReadFile(receiptPath(b.home, image)); err == nil {
		var cached receipt
		if json.Unmarshal(data, &cached) == nil && cached.Image == image && cached.Version == meta.Version {
			if info, err := inspectImage(ctx, b.docker, image); err == nil && info.ID == cached.Digest && info.Config.Labels["agent-pm.version"] == meta.Version && info.Config.Labels["agent-pm.key"] == key {
				return BuildResult{Image: image, Digest: info.ID}, nil
			}
		}
	}
	userImage := "agent-pm-user:" + key
	if _, err := b.docker.Run(ctx, []string{"build", "--progress=plain", "--tag", userImage, userDir}, nil); err != nil {
		return BuildResult{}, err
	}
	user, err := inspectImage(ctx, b.docker, userImage)
	if err != nil {
		return BuildResult{}, err
	}
	args := []string{"build", "--progress=plain", "--build-arg", "USER_IMAGE=" + user.ID, "--build-arg", "AGENT_PM_VERSION=" + meta.Version, "--label", "agent-pm.key=" + key, "--tag", image, packageDir}
	if _, err := b.docker.Run(ctx, args, nil); err != nil {
		return BuildResult{}, err
	}
	info, err := inspectImage(ctx, b.docker, image)
	if err != nil {
		return BuildResult{}, err
	}
	if info.Config.Labels["agent-pm.version"] != meta.Version || info.Config.Labels["agent-pm.key"] != key {
		return BuildResult{}, errors.New("built image labels do not match package")
	}
	record := receipt{Image: image, Digest: info.ID, Version: meta.Version}
	data, _ = json.Marshal(record)
	path := receiptPath(b.home, image)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return BuildResult{}, err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".receipt-")
	if err != nil {
		return BuildResult{}, err
	}
	defer os.Remove(file.Name())
	_, writeErr := file.Write(data)
	if err := errors.Join(writeErr, file.Close()); err != nil {
		return BuildResult{}, err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return BuildResult{}, err
	}
	return BuildResult{Image: image, Digest: info.ID, Rebuilt: true}, nil
}
