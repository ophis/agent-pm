// Package github is the broker's GitHub App client: the App JWT, its installations, installation tokens and the
// repositories each reaches, and its bot user's ID.
// Its errors carry an HTTP status at most, never a response body or a token.
package github

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"time"
)

const (
	perPage   = 100
	maxPages  = 100
	maxBody   = 1 << 20
	apiTarget = "https://api.github.com"
)

// Login is a GitHub account login, safe as a file name and argv element.
var Login = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)

// fullName is a repository's owner/name.
var fullName = regexp.MustCompile(`^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`)

// permissions are every token's, the App's fixed set.
var permissions = map[string]string{"contents": "write", "pull_requests": "write", "checks": "read", "actions": "read"}

type Client struct {
	Base string
	HTTP *http.Client
	Now  func() time.Time
}

func New() Client {
	return Client{Base: apiTarget, HTTP: &http.Client{Timeout: 30 * time.Second}, Now: time.Now}
}

type Installation struct {
	ID      int64
	Account string
}

// StatusError is a response with an unexpected HTTP status.
type StatusError struct{ Status int }

func (e *StatusError) Error() string { return "GitHub answered HTTP " + strconv.Itoa(e.Status) }

type transportError struct{ op string }

func (e *transportError) Error() string { return "GitHub unreachable: " + e.op }

// Transport reports whether err got no HTTP response at all (offline, timeout).
func Transport(err error) bool {
	var te *transportError
	return errors.As(err, &te)
}

// ParseKey reads an App private key: the keychain item's base64 of the PEM file, or the PEM itself.
func ParseKey(data []byte) (*rsa.PrivateKey, error) {
	if decoded, err := base64.StdEncoding.DecodeString(string(bytes.TrimSpace(data))); err == nil {
		data = decoded
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("App private key is not PEM")
	}
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	key, ok := parsed.(*rsa.PrivateKey)
	if err != nil || !ok {
		return nil, errors.New("App private key is not an RSA key")
	}
	return key, nil
}

// JWT is the App's 9-minute RS256 token, backdated a minute for clock drift.
func (c Client) JWT(appID string, key *rsa.PrivateKey) (string, error) {
	part := func(v any) string {
		data, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(data)
	}
	now := c.Now().Unix()
	signed := part(map[string]string{"alg": "RS256", "typ": "JWT"}) + "." + part(map[string]any{"iat": now - 60, "exp": now + 540, "iss": appID})
	sum := sha256.Sum256([]byte(signed))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	if err != nil {
		return "", err
	}
	return signed + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

// call sends one request authorized by bearer ("": none) and decodes a want-status JSON answer into out (nil: no body
// read).
func (c Client) call(ctx context.Context, method, path, bearer string, body any, want int, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Base+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "agent-pm-broker")
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return &transportError{method + " " + path}
	}
	defer resp.Body.Close()
	if resp.StatusCode != want {
		return &StatusError{resp.StatusCode}
	}
	if out == nil {
		return nil
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBody)).Decode(out); err != nil {
		return fmt.Errorf("GitHub answered unreadable JSON to %s %s", method, path)
	}
	return nil
}

// Installations lists every installation of the App, reading every page.
func (c Client) Installations(ctx context.Context, jwt string) ([]Installation, error) {
	var all []Installation
	for page := 1; ; page++ {
		var found []struct {
			ID      int64 `json:"id"`
			Account struct {
				Login string `json:"login"`
			} `json:"account"`
		}
		path := fmt.Sprintf("/app/installations?per_page=%d&page=%d", perPage, page)
		if err := c.call(ctx, http.MethodGet, path, jwt, nil, http.StatusOK, &found); err != nil {
			return nil, err
		}
		for _, f := range found {
			if f.ID <= 0 || !Login.MatchString(f.Account.Login) {
				return nil, errors.New("GitHub listed an installation without a valid id and account login")
			}
			all = append(all, Installation{f.ID, f.Account.Login})
		}
		if len(found) < perPage {
			return all, nil
		}
	}
}

// Issue creates an installation token for every repository of installation id, with the App's fixed permissions.
func (c Client) Issue(ctx context.Context, jwt string, id int64) (string, time.Time, error) {
	var got struct {
		Token     string `json:"token"`
		ExpiresAt string `json:"expires_at"`
	}
	path := fmt.Sprintf("/app/installations/%d/access_tokens", id)
	if err := c.call(ctx, http.MethodPost, path, jwt, map[string]any{"permissions": permissions}, http.StatusCreated, &got); err != nil {
		return "", time.Time{}, err
	}
	expires, err := time.Parse(time.RFC3339, got.ExpiresAt)
	if got.Token == "" || err != nil {
		return "", time.Time{}, errors.New("GitHub answered a token without a token or expiry")
	}
	return got.Token, expires.UTC(), nil
}

// Repositories is what installation token reaches: its installation's repository_selection and every repository's
// full name, sorted, reading every page up to maxPages.
func (c Client) Repositories(ctx context.Context, token string) (string, []string, error) {
	names := []string{}
	var selection string
	for page := 1; page <= maxPages; page++ {
		var got struct {
			Total        int    `json:"total_count"`
			Selection    string `json:"repository_selection"`
			Repositories []struct {
				FullName string `json:"full_name"`
			} `json:"repositories"`
		}
		path := fmt.Sprintf("/installation/repositories?per_page=%d&page=%d", perPage, page)
		if err := c.call(ctx, http.MethodGet, path, token, nil, http.StatusOK, &got); err != nil {
			return "", nil, err
		}
		if got.Selection != "all" && got.Selection != "selected" || selection != "" && got.Selection != selection {
			return "", nil, errors.New("GitHub answered no valid repository_selection")
		}
		selection = got.Selection
		for _, r := range got.Repositories {
			if !fullName.MatchString(r.FullName) {
				return "", nil, errors.New("GitHub listed a repository without a valid full name")
			}
			names = append(names, r.FullName)
		}
		if len(got.Repositories) < perPage || len(names) >= got.Total {
			slices.Sort(names)
			return selection, names, nil
		}
	}
	return "", nil, fmt.Errorf("GitHub listed more than %d repositories", maxPages*perPage)
}

// Revoke invalidates token; 401 and 404 mean it is already invalid.
func (c Client) Revoke(ctx context.Context, token string) error {
	err := c.call(ctx, http.MethodDelete, "/installation/token", token, nil, http.StatusNoContent, nil)
	var se *StatusError
	if errors.As(err, &se) && (se.Status == http.StatusUnauthorized || se.Status == http.StatusNotFound) {
		return nil
	}
	return err
}

// AppSlug is the slug of the App jwt signs for.
func (c Client) AppSlug(ctx context.Context, jwt string) (string, error) {
	var got struct {
		Slug string `json:"slug"`
	}
	if err := c.call(ctx, http.MethodGet, "/app", jwt, nil, http.StatusOK, &got); err != nil {
		return "", err
	}
	if !Login.MatchString(got.Slug) {
		return "", errors.New("GitHub answered no valid App slug")
	}
	return got.Slug, nil
}

// BotUser is the user ID of bot login (`<app>[bot]`), from the public user API, sent without a credential.
func (c Client) BotUser(ctx context.Context, login string) (int64, error) {
	var got struct {
		Login string `json:"login"`
		ID    int64  `json:"id"`
		Type  string `json:"type"`
	}
	if err := c.call(ctx, http.MethodGet, "/users/"+url.PathEscape(login), "", nil, http.StatusOK, &got); err != nil {
		return 0, err
	}
	if got.Login != login || got.Type != "Bot" || got.ID < 1 {
		return 0, errors.New("GitHub answered no bot user with that login and a valid id")
	}
	return got.ID, nil
}
