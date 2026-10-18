package broker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"syscall"
	"time"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/github"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

// The broker-only items: a token record per container, and one revocation backlog.
const (
	tokensService      = "agent-pm-tokens"
	revocationsService = "agent-pm-revocations"
	revocationsAccount = "pending"
	tokenDir           = docker.SecretsDir + "/github"
)

// writeScript replaces $1's token file with stdin; the temporary name is a dot file, which no reader matches.
const writeScript = "mkdir -p -m 0700 " + tokenDir + " && umask 0377 && rm -f " + tokenDir + `/."$1".new && cat > ` + tokenDir +
	`/."$1".new && mv -f ` + tokenDir + `/."$1".new ` + tokenDir + `/"$1"`

type tokenEntry struct {
	Account      string `json:"account"`
	Installation int64  `json:"installation"`
	Token        string `json:"token"`
	ExpiresAt    string `json:"expires_at"`
}

type tokenRecord struct {
	App    string       `json:"app"`
	Tokens []tokenEntry `json:"tokens"`
}

type pending struct {
	Container string `json:"container"`
	Account   string `json:"account"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

// AppKeys reads a GitHub App's private-key item.
type AppKeys interface {
	AppAccount(app string) (string, error)
	AppKey(app, account string) ([]byte, error)
}

// GitHub issues, refreshes and revokes a container's installation tokens. Every token it issues is in the
// container's record or the backlog until revoked or expired; revocation uses only those values, never the
// container's files. Lock guards each read-modify-write of either item, never a GitHub request.
type GitHub struct {
	Keychain SecretKeychain
	Apps     AppKeys
	API      github.Client
	Lock     func() (unlock func(), err error)
}

// FileLock is an exclusive flock on path, created 0600 in a 0700 directory.
func FileLock(path string) func() (func(), error) {
	return func() (func(), error) {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return nil, err
		}
		f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
		if err != nil {
			return nil, err
		}
		if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
			f.Close()
			return nil, err
		}
		return func() { f.Close() }, nil
	}
}

var errLock = errors.New("token lock")

func tokensError(err error) *protocol.Error {
	if errors.Is(err, errLock) {
		return protocol.New(protocol.KeychainFailed, "Taking the GitHub token lock ~/.agent-pm/docker/tokens.lock failed.")
	}
	return protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub tokens failed.")
}

func unrevoked(container string) *protocol.Error {
	return protocol.New(protocol.KeychainFailed, "The GitHub tokens of "+container+" could not be revoked; they expire within an hour.")
}

func apiError(err error) *protocol.Error {
	return protocol.New(protocol.GitHubFailed, "GitHub request failed: "+err.Error()+".")
}

func loginError(app string) *protocol.Error {
	return protocol.New(protocol.LoginRequired, "No usable GitHub App private key "+app+": run agent-pm-broker login github "+app+".")
}

func (g GitHub) locked(f func() error) error {
	unlock, err := g.Lock()
	if err != nil {
		return fmt.Errorf("%w: %v", errLock, err)
	}
	defer unlock()
	return f()
}

func (g GitHub) readRecord(container string) (tokenRecord, bool, error) {
	var rec tokenRecord
	data, err := g.Keychain.Secret(tokensService, container)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return rec, false, nil
	case err != nil:
		return rec, false, err
	}
	return rec, true, json.Unmarshal(data, &rec)
}

func (g GitHub) writeRecord(container string, rec tokenRecord) error {
	if len(rec.Tokens) == 0 {
		return g.Keychain.DeleteSecret(tokensService, container)
	}
	data, _ := json.Marshal(rec)
	defer erase(data)
	return g.Keychain.PutSecret(tokensService, container, data)
}

// editRecord changes container's record under the lock; f gets whether it exists, and a missing one is never created
// unless f adds tokens and create is set.
func (g GitHub) editRecord(container string, create bool, f func(rec *tokenRecord, found bool)) (bool, error) {
	var found bool
	err := g.locked(func() error {
		rec, ok, err := g.readRecord(container)
		if err != nil {
			return err
		}
		found = ok
		f(&rec, ok)
		if !ok && !create {
			return nil
		}
		return g.writeRecord(container, rec)
	})
	return found, err
}

func (g GitHub) readBacklog() ([]pending, error) {
	var list []pending
	data, err := g.Keychain.Secret(revocationsService, revocationsAccount)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return nil, nil
	case err != nil:
		return nil, err
	}
	return list, json.Unmarshal(data, &list)
}

// editBacklog changes the backlog; the caller holds the lock.
func (g GitHub) editBacklog(f func([]pending) []pending) error {
	list, err := g.readBacklog()
	if err != nil {
		return err
	}
	list = f(list)
	if len(list) == 0 {
		return g.Keychain.DeleteSecret(revocationsService, revocationsAccount)
	}
	data, _ := json.Marshal(list)
	defer erase(data)
	return g.Keychain.PutSecret(revocationsService, revocationsAccount, data)
}

func tokenIn(entries []tokenEntry) func(string) bool {
	return func(t string) bool {
		return slices.ContainsFunc(entries, func(e tokenEntry) bool { return e.Token == t })
	}
}

// discard takes entries out of container's record (all of it, read in the same lock section, when whole) and
// revokes them through the backlog. With the backlog unreadable, or the lock failing, it revokes them directly;
// with the lock failing it queues the ones it could not revoke without the lock, so none is left untracked, then
// takes them out of the record.
func (g GitHub) discard(ctx context.Context, container string, entries []tokenEntry, whole bool) error {
	var backlogErr error
	ran := false
	err := g.locked(func() error {
		ran = true
		if whole {
			if rec, found, err := g.readRecord(container); err == nil && found {
				in := tokenIn(entries)
				entries = append(slices.Clone(entries), slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })...)
			}
		}
		backlogErr = g.editBacklog(func(list []pending) []pending {
			for _, e := range entries {
				list = append(list, pending{container, e.Account, e.Token, e.ExpiresAt})
			}
			return list
		})
		if whole {
			return g.Keychain.DeleteSecret(tokensService, container)
		}
		rec, found, err := g.readRecord(container)
		if err != nil || !found {
			return err
		}
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return tokenIn(entries)(e.Token) })
		return g.writeRecord(container, rec)
	})
	if !ran && whole {
		if rec, found, err := g.readRecord(container); err == nil && found {
			in := tokenIn(entries)
			entries = append(slices.Clone(entries), slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })...)
		}
	}
	if backlogErr != nil || !ran {
		var left []pending
		for _, e := range entries {
			if g.API.Revoke(ctx, e.Token) != nil {
				left = append(left, pending{container, e.Account, e.Token, e.ExpiresAt})
			}
		}
		if !ran {
			if len(left) > 0 && g.editBacklog(func(list []pending) []pending { return append(list, left...) }) != nil {
				return unrevoked(container) // the record keeps them tracked
			}
			g.untrack(container, entries, whole)
		} else if len(left) > 0 {
			return unrevoked(container)
		}
	} else {
		_, _ = g.Retry(ctx)
	}
	if err != nil {
		return tokensError(err)
	}
	return nil
}

// untrack takes entries, revoked or queued, out of container's record without the lock: best effort, as the lock
// is what failed.
func (g GitHub) untrack(container string, entries []tokenEntry, whole bool) {
	if whole {
		_ = g.Keychain.DeleteSecret(tokensService, container)
		return
	}
	if rec, found, err := g.readRecord(container); err == nil && found {
		in := tokenIn(entries)
		rec.Tokens = slices.DeleteFunc(rec.Tokens, func(e tokenEntry) bool { return in(e.Token) })
		_ = g.writeRecord(container, rec)
	}
}

// Ready checks, without reading the key, that app is the broker's App and its key item is in place.
func (g GitHub) Ready(app string) error {
	if !GitHubApps[app] {
		return protocol.New(protocol.ManifestRejected, "Manifest rejected: github app must be agent-pm-bot.")
	}
	_, err := g.appID(app)
	return err
}

func (g GitHub) appID(app string) (string, error) {
	account, err := g.Apps.AppAccount(app)
	switch {
	case errors.Is(err, ErrNotFound), errors.Is(err, ErrAmbiguous), err == nil && !appID.MatchString(account):
		return "", loginError(app)
	case err != nil:
		return "", protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub App private key failed.")
	}
	return account, nil
}

func (g GitHub) jwt(app string) (string, error) {
	id, err := g.appID(app)
	if err != nil {
		return "", err
	}
	data, err := g.Apps.AppKey(app, id)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return "", loginError(app)
	case err != nil:
		return "", protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub App private key failed.")
	}
	key, err := github.ParseKey(data)
	if err != nil {
		return "", protocol.New(protocol.GitHubFailed, "GitHub App private key "+app+" unusable: "+err.Error()+".")
	}
	jwt, err := g.API.JWT(id, key)
	if err != nil {
		return "", protocol.New(protocol.GitHubFailed, "Signing the GitHub App token failed.")
	}
	return jwt, nil
}

// write puts e's token into the container through exec's stdin; errors wrap docker.ErrUnreachable or are opaque.
func write(exec func([]string, []byte) error, e tokenEntry) error {
	if !github.Login.MatchString(e.Account) {
		return fmt.Errorf("account %q is not a GitHub login", e.Account)
	}
	token := []byte(e.Token)
	defer erase(token)
	err := exec([]string{"/bin/sh", "-c", writeScript, "sh", e.Account}, token)
	if errors.Is(err, docker.ErrUnreachable) {
		return fmt.Errorf("GitHub token injection: %w", docker.ErrUnreachable)
	}
	if err != nil {
		return errors.New("writing a GitHub token into the container failed")
	}
	return nil
}

func expiries(entries []tokenEntry) []protocol.Expiry {
	out := []protocol.Expiry{}
	for _, e := range entries {
		out = append(out, protocol.Expiry{Account: e.Account, ExpiresAt: e.ExpiresAt})
	}
	slices.SortFunc(out, func(a, b protocol.Expiry) int {
		if a.Account < b.Account {
			return -1
		}
		if a.Account > b.Account {
			return 1
		}
		return 0
	})
	return out
}

// Issue gives container one token per installation of app, recording each before writing it in; on any failure
// it revokes every token it issued.
func (g GitHub) Issue(ctx context.Context, app, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	jwt, err := g.jwt(app)
	if err != nil {
		return nil, err
	}
	installs, err := g.API.Installations(ctx, jwt)
	if err != nil {
		return nil, apiError(err)
	}
	var issued []tokenEntry
	fail := func(err error) ([]protocol.Expiry, error) {
		if len(issued) > 0 {
			_ = g.discard(ctx, container, issued, true)
		}
		return nil, err
	}
	for _, in := range installs {
		token, expires, err := g.API.Issue(ctx, jwt, in.ID)
		if err != nil {
			return fail(apiError(err))
		}
		e := tokenEntry{in.Account, in.ID, token, expires.Format(time.RFC3339)}
		issued = append(issued, e)
		if _, err := g.editRecord(container, true, func(rec *tokenRecord, _ bool) {
			rec.App, rec.Tokens = app, append(rec.Tokens, e)
		}); err != nil {
			return fail(tokensError(err))
		}
	}
	for _, e := range issued {
		if err := write(exec, e); err != nil {
			return fail(err)
		}
	}
	return expiries(issued), nil
}

// Refresh replaces each of container's tokens: a new one is recorded and written in before the old is revoked.
func (g GitHub) Refresh(ctx context.Context, app, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	var rec tokenRecord
	var found bool
	if err := g.locked(func() (err error) {
		rec, found, err = g.readRecord(container)
		return err
	}); err != nil {
		return nil, tokensError(err)
	}
	if !found {
		return nil, protocol.New(protocol.RefreshFailed, "No GitHub tokens recorded for "+container+".")
	}
	jwt, err := g.jwt(app)
	if err != nil {
		return nil, err
	}
	var fresh []tokenEntry
	for _, old := range rec.Tokens {
		token, expires, err := g.API.Issue(ctx, jwt, old.Installation)
		if err != nil {
			return nil, apiError(err)
		}
		e := tokenEntry{old.Account, old.Installation, token, expires.Format(time.RFC3339)}
		found, err := g.editRecord(container, false, func(rec *tokenRecord, found bool) {
			if found {
				rec.Tokens = append(rec.Tokens, e)
			}
		})
		if err != nil || !found {
			_ = g.discard(ctx, container, []tokenEntry{e}, false)
			if err != nil {
				return nil, tokensError(err)
			}
			return nil, protocol.New(protocol.RefreshFailed, container+" was closed during the refresh.")
		}
		if err := write(exec, e); err != nil {
			_ = g.discard(ctx, container, []tokenEntry{e}, false)
			return nil, err
		}
		if g.API.Revoke(ctx, old.Token) != nil {
			_ = g.discard(ctx, container, []tokenEntry{old}, false)
		} else {
			_, _ = g.editRecord(container, false, func(rec *tokenRecord, _ bool) {
				rec.Tokens = slices.DeleteFunc(rec.Tokens, func(t tokenEntry) bool { return t.Token == old.Token })
			})
		}
		fresh = append(fresh, e)
	}
	return expiries(fresh), nil
}

// Revoke revokes every token of container's record and deletes it. An unreadable record is deleted best-effort.
func (g GitHub) Revoke(ctx context.Context, container string) error {
	var found bool
	err := g.locked(func() (err error) {
		_, found, err = g.readRecord(container)
		return err
	})
	if errors.Is(err, errLock) {
		return g.discard(ctx, container, nil, true)
	}
	if err != nil {
		_ = g.Keychain.DeleteSecret(tokensService, container)
		return unrevoked(container)
	}
	if !found {
		_, _ = g.Retry(ctx)
		return nil
	}
	return g.discard(ctx, container, nil, true)
}

// Retry revokes the backlog: expired entries are dropped unrevoked, and the first request that gets no answer
// stops the run, keeping the rest for next time.
func (g GitHub) Retry(ctx context.Context) ([]protocol.Pending, error) {
	var list []pending
	if err := g.locked(func() (err error) {
		list, err = g.readBacklog()
		return err
	}); err != nil {
		return nil, tokensError(err)
	}
	now := g.API.Now()
	var done []string
	for _, p := range list {
		expires, err := time.Parse(time.RFC3339, p.ExpiresAt)
		if err != nil || !now.Before(expires) {
			done = append(done, p.Token)
			continue
		}
		err = g.API.Revoke(ctx, p.Token)
		if err == nil {
			done = append(done, p.Token)
		} else if github.Transport(err) {
			break
		}
	}
	if len(done) > 0 {
		if err := g.locked(func() error {
			return g.editBacklog(func(list []pending) []pending {
				return slices.DeleteFunc(list, func(p pending) bool { return slices.Contains(done, p.Token) })
			})
		}); err != nil {
			return nil, tokensError(err)
		}
	}
	out := []protocol.Pending{}
	for _, p := range list {
		if !slices.Contains(done, p.Token) {
			out = append(out, protocol.Pending{Container: p.Container, Account: p.Account, ExpiresAt: p.ExpiresAt})
		}
	}
	return out, nil
}
