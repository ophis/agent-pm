package broker

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/ophis/agent-pm/broker/internal/codex"
	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

const (
	codexService        = "agent-pm-codex"
	codexAccount        = "auth"
	maxCodexAuth        = 64 * 1024
	codexLoginCommand   = "agent-pm-broker login codex"
	codexInstallCommand = "sudo " + InstalledPath + " install-codex <version>"
	codexUpdateCommand  = "sudo " + InstalledPath + " update-codex <version>"
)

func codexRepair(err error) string {
	if errors.Is(err, codex.ErrNotInstalled) {
		return codexInstallCommand
	}
	return codexUpdateCommand
}

type codexNative interface {
	Verify() (codex.Installation, error)
	Run(context.Context, string, ...string) error
	Lock(context.Context) (func(), error)
}

type codexKeychain interface {
	AppKeychain
	Secret(string, string) ([]byte, error)
	CreateCodex() error
	WriteCodex([]byte) error
}

type Codex struct {
	native   codexNative
	keychain codexKeychain
	password func() ([]byte, error)
	tempRoot string
	now      func() time.Time
	uid      func() int
}

func NewCodex(home string, password func() ([]byte, error)) Codex {
	return Codex{native: codex.New(), keychain: SystemKeychain{Home: home}, password: password, tempRoot: "/private/tmp", now: time.Now, uid: os.Geteuid}
}

type codexAuthTokens struct {
	ID      string `json:"id_token"`
	Access  string `json:"access_token"`
	Refresh string `json:"refresh_token"`
	Account string `json:"account_id"`
}

type codexAuthDocument struct {
	Mode        string          `json:"auth_mode"`
	APIKey      *string         `json:"OPENAI_API_KEY"`
	Tokens      codexAuthTokens `json:"tokens"`
	LastRefresh string          `json:"last_refresh"`
}

func parseCodexAuth(data []byte) (time.Time, error) {
	invalid := errors.New("invalid Codex subscription credentials; rerun " + codexLoginCommand)
	if len(data) == 0 || len(data) > maxCodexAuth {
		return time.Time{}, invalid
	}
	var auth codexAuthDocument
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&auth) != nil {
		return time.Time{}, invalid
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return time.Time{}, invalid
	}
	if auth.APIKey != nil || (auth.Mode != "" && auth.Mode != "chatgpt") {
		return time.Time{}, invalid
	}
	for _, s := range []string{auth.Tokens.ID, auth.Tokens.Access, auth.Tokens.Refresh, auth.Tokens.Account} {
		if len(s) == 0 || len(s) > maxToken || strings.ContainsFunc(s, func(r rune) bool { return r <= 0x20 || r == 0x7f }) {
			return time.Time{}, invalid
		}
	}
	if _, err := time.Parse(time.RFC3339, auth.LastRefresh); err != nil {
		return time.Time{}, invalid
	}
	parts := strings.Split(auth.Tokens.Access, ".")
	if len(parts) != 3 || parts[0] == "" || parts[2] == "" {
		return time.Time{}, invalid
	}
	claims, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return time.Time{}, invalid
	}
	defer erase(claims)
	var token struct {
		Exp int64 `json:"exp"`
	}
	if json.Unmarshal(claims, &token) != nil || token.Exp <= 0 || token.Exp > 253402300799 {
		return time.Time{}, invalid
	}
	return time.Unix(token.Exp, 0).UTC(), nil
}

func readCodexAuth(home string) ([]byte, error) {
	f, err := os.OpenFile(filepath.Join(home, "auth.json"), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, errors.New("Codex did not create a private credential file")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, errors.New("cannot inspect Codex credential file")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || info.Mode().Perm()&0o077 != 0 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || info.Size() > maxCodexAuth {
		return nil, errors.New("Codex credential file is not private")
	}
	data, err := io.ReadAll(io.LimitReader(f, maxCodexAuth+1))
	if err != nil || len(data) > maxCodexAuth {
		erase(data)
		return nil, errors.New("cannot read Codex credential file")
	}
	return data, nil
}

func (c Codex) Login(ctx context.Context, out io.Writer) error {
	if c.uid() == 0 {
		return errors.New("run " + codexLoginCommand + " as your ordinary user, without sudo")
	}
	if _, err := c.native.Verify(); err != nil {
		return errors.New("pinned Codex unavailable; run " + codexRepair(err))
	}
	unlock, err := c.native.Lock(ctx)
	if err != nil {
		return errors.New("cannot lock Codex login; install pinned Codex or retry")
	}
	defer unlock()
	_, err = c.keychain.AppAccess(codexService, codexAccount)
	missing := errors.Is(err, ErrNotFound)
	if err != nil && !missing {
		return errors.New("cannot inspect Codex keychain access; retry " + codexLoginCommand)
	}
	var previous []byte
	defer func() { erase(previous) }()
	home, err := os.MkdirTemp(c.tempRoot, "agent-pm-codex-")
	if err != nil {
		return errors.New("cannot create private Codex login directory")
	}
	defer os.RemoveAll(home)
	if err := os.Chmod(home, 0o700); err != nil {
		return errors.New("cannot protect Codex login directory")
	}
	if _, err := fmt.Fprintln(out, "Opening Codex browser login..."); err != nil {
		return err
	}
	if err := c.native.Run(ctx, home, "-c", `cli_auth_credentials_store="file"`, "-c", `forced_login_method="chatgpt"`, "login"); err != nil {
		return errors.New("Codex browser login failed or was canceled; retry " + codexLoginCommand)
	}
	data, err := readCodexAuth(home)
	defer erase(data)
	if err != nil {
		return err
	}
	expires, err := parseCodexAuth(data)
	if err != nil {
		return err
	}
	if !c.now().Before(expires) {
		return errors.New("Codex returned expired credentials; retry " + codexLoginCommand)
	}
	if err := ctx.Err(); err != nil {
		return errors.New("Codex login canceled")
	}
	if err := os.RemoveAll(home); err != nil {
		return errors.New("cannot remove private Codex login directory; login was not saved")
	}
	if missing {
		if c.keychain.CreateCodex() != nil {
			return errors.New("cannot create Codex keychain item")
		}
	}
	var password []byte
	var passwordErr error
	passwordRead := false
	defer func() { erase(password) }()
	secure := func() error {
		access, err := c.keychain.AppAccess(codexService, codexAccount)
		if err != nil {
			return errors.New("cannot inspect Codex keychain access")
		}
		if onlyBroker(access) != nil {
			if !passwordRead {
				passwordRead = true
				password, passwordErr = c.password()
			}
			if passwordErr != nil || trust(codexService, codexAccount, c.keychain, access, password) != nil {
				return errors.New("Codex keychain trust incomplete; retry " + codexLoginCommand)
			}
		}
		return c.trusted()
	}
	if err := secure(); err != nil {
		return err
	}
	if !missing && len(previous) == 0 {
		old, err := c.keychain.Secret(codexService, codexAccount)
		if err != nil {
			erase(old)
			return errors.New("cannot read existing Codex login; retry " + codexLoginCommand)
		}
		if expiry, err := parseCodexAuth(old); err == nil && c.now().Before(expiry) {
			previous = old
		} else {
			erase(old)
		}
	}
	if ctx.Err() != nil {
		return errors.New("Codex login canceled")
	}
	if c.keychain.WriteCodex(data) != nil {
		return errors.New("cannot save Codex login; retry " + codexLoginCommand)
	}
	if c.trusted() != nil || !c.matches(data) {
		if len(previous) > 0 {
			if secure() != nil || c.keychain.WriteCodex(previous) != nil || secure() != nil || !c.matches(previous) {
				return errors.New("Codex login verification failed and previous login could not be restored; retry " + codexLoginCommand)
			}
			return errors.New("Codex login verification failed; previous login restored; retry " + codexLoginCommand)
		}
		return errors.New("Codex login verification failed; retry " + codexLoginCommand)
	}
	_, err = fmt.Fprintln(out, "Codex login saved; keychain trusts only the broker")
	return err
}

func (c Codex) matches(expected []byte) bool {
	saved, err := c.keychain.Secret(codexService, codexAccount)
	defer erase(saved)
	return err == nil && bytes.Equal(saved, expected)
}

func (c Codex) trusted() error {
	a, err := c.keychain.AppAccess(codexService, codexAccount)
	if err != nil || onlyBroker(a) != nil {
		return errors.New("Codex keychain trust incomplete; retry " + codexLoginCommand)
	}
	return nil
}

func (c Codex) containerAuth(ctx context.Context, use func(codexAuthDocument, time.Time) error) error {
	if _, err := c.native.Verify(); err != nil {
		return protocol.New(protocol.LoginRequired, "Pinned Codex unavailable; run "+codexRepair(err))
	}
	unlock, err := c.native.Lock(ctx)
	if err != nil {
		return protocol.New(protocol.LoginRequired, "Cannot lock Codex login; retry "+codexLoginCommand)
	}
	defer unlock()
	if err := c.trusted(); err != nil {
		return protocol.New(protocol.LoginRequired, "Codex login unavailable; run "+codexLoginCommand)
	}
	data, err := c.keychain.Secret(codexService, codexAccount)
	defer erase(data)
	if err != nil {
		return protocol.New(protocol.LoginRequired, "Codex login unavailable; run "+codexLoginCommand)
	}
	expires, err := parseCodexAuth(data)
	if err != nil || !c.now().Before(expires) {
		return protocol.New(protocol.LoginRequired, "Codex authentication expired or invalid; run "+codexLoginCommand+"; token refresh is not supported yet")
	}
	var auth codexAuthDocument
	if err := json.Unmarshal(data, &auth); err != nil {
		return protocol.New(protocol.LoginRequired, "Invalid Codex login; run "+codexLoginCommand)
	}
	return use(auth, expires)
}

func (c Codex) Ready(ctx context.Context) error {
	return c.containerAuth(ctx, func(codexAuthDocument, time.Time) error { return nil })
}

func (c Codex) Inject(ctx context.Context, exec func([]string, []byte) error) (string, error) {
	var expiry string
	err := c.containerAuth(ctx, func(auth codexAuthDocument, expires time.Time) error {
		auth.Mode = "chatgptAuthTokens"
		auth.Tokens.ID, auth.Tokens.Refresh = auth.Tokens.Access, ""
		data, err := json.Marshal(auth)
		if err != nil {
			return protocol.New(protocol.StartFailed, "Cannot encode Codex container credentials")
		}
		defer erase(data)
		err = exec([]string{"/opt/agent-pm/venv/bin/python", "-I", "/opt/agent-pm/lib/bootstrap.py", "codex-auth"}, data)
		if errors.Is(err, docker.ErrUnreachable) {
			return docker.ErrUnreachable
		}
		if err != nil {
			return protocol.New(protocol.StartFailed, "Writing Codex credentials into the container failed")
		}
		expiry = expires.Format(time.RFC3339)
		return nil
	})
	return expiry, err
}

func (c Codex) Status(ctx context.Context) protocol.CodexStatus {
	s := protocol.CodexStatus{AllowedVersions: []string{}, State: "installation_invalid"}
	installed, err := c.native.Verify()
	if err != nil {
		s.Repair = codexRepair(err)
		return s
	}
	s.Version, s.Path, s.SHA256, s.AllowedVersions = installed.Version, installed.Path, installed.SHA256, installed.AllowedVersions
	s.State, s.Repair = "login_unavailable", codexLoginCommand
	ctx, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	defer cancel()
	unlock, err := c.native.Lock(ctx)
	if err != nil {
		s.State = "busy"
		s.Repair = "retry agent-pm-broker status"
		return s
	}
	defer unlock()
	if c.trusted() != nil {
		return s
	}
	data, err := c.keychain.Secret(codexService, codexAccount)
	defer erase(data)
	if err != nil {
		return s
	}
	expires, err := parseCodexAuth(data)
	if err != nil {
		return s
	}
	s.ExpiresAt = expires.Format(time.RFC3339)
	if !c.now().Before(expires) {
		s.State = "expired"
		return s
	}
	s.State, s.Repair, s.LoggedIn = "ready", "", true
	return s
}
