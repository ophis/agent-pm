package github

import (
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
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

var now = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)

func testKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func client(t *testing.T, h http.HandlerFunc) Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return Client{Base: srv.URL, HTTP: srv.Client(), Now: func() time.Time { return now }}
}

func TestParseKey(t *testing.T) {
	key := testKey(t)
	pkcs1 := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	pkcs8 := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	for name, data := range map[string][]byte{
		"pkcs1":        pkcs1,
		"pkcs8":        pkcs8,
		"base64 pkcs1": []byte(base64.StdEncoding.EncodeToString(pkcs1)),
		"base64 crlf":  []byte(base64.StdEncoding.EncodeToString(pkcs8) + "\r\n"),
	} {
		got, err := ParseKey(data)
		if err != nil || !got.Equal(key) {
			t.Errorf("%s: %v", name, err)
		}
	}
	for name, data := range map[string][]byte{
		"empty":   nil,
		"garbage": []byte("not a key"),
		"cert":    pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: []byte("x")}),
	} {
		if _, err := ParseKey(data); err == nil {
			t.Errorf("%s: parsed", name)
		}
	}
}

func TestJWT(t *testing.T) {
	key := testKey(t)
	jwt, err := Client{Now: func() time.Time { return now }}.JWT("5352992", key)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(jwt, ".")
	if len(parts) != 3 {
		t.Fatalf("parts %d", len(parts))
	}
	decode := func(s string) []byte {
		b, err := base64.RawURLEncoding.DecodeString(s)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	var header, claims map[string]any
	if json.Unmarshal(decode(parts[0]), &header) != nil || json.Unmarshal(decode(parts[1]), &claims) != nil {
		t.Fatal("bad json")
	}
	if header["alg"] != "RS256" || header["typ"] != "JWT" {
		t.Errorf("header %v", header)
	}
	if claims["iss"] != "5352992" || claims["iat"] != float64(now.Unix()-60) || claims["exp"] != float64(now.Unix()+540) {
		t.Errorf("claims %v", claims)
	}
	sum := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(&key.PublicKey, crypto.SHA256, sum[:], decode(parts[2])); err != nil {
		t.Errorf("signature: %v", err)
	}
}

func TestInstallationsPaginates(t *testing.T) {
	var pages []string
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/app/installations" || r.Header.Get("Authorization") != "Bearer J" ||
			r.Header.Get("Accept") != "application/vnd.github+json" || r.Header.Get("X-GitHub-Api-Version") != "2022-11-28" {
			t.Errorf("request %s %s %v", r.Method, r.URL, r.Header)
		}
		pages = append(pages, r.URL.Query().Get("page")+"/"+r.URL.Query().Get("per_page"))
		n := 100
		if r.URL.Query().Get("page") == "2" {
			n = 1
		}
		var items []string
		for i := range n {
			items = append(items, fmt.Sprintf(`{"id":%d,"account":{"login":"owner-%d"}}`, i+1, i))
		}
		_, _ = io.WriteString(w, "["+strings.Join(items, ",")+"]")
	})
	got, err := c.Installations(context.Background(), "J")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 101 || got[0] != (Installation{ID: 1, Account: "owner-0"}) || strings.Join(pages, " ") != "1/100 2/100" {
		t.Fatalf("got %d %v pages %v", len(got), got[:1], pages)
	}
}

func TestInstallationsRejectsBadLogin(t *testing.T) {
	for _, login := range []string{"../x", ".hidden", "", "-dash", strings.Repeat("a", 40), "a b"} {
		c := client(t, func(w http.ResponseWriter, _ *http.Request) {
			_, _ = fmt.Fprintf(w, `[{"id":1,"account":{"login":%q}}]`, login)
		})
		if _, err := c.Installations(context.Background(), "J"); err == nil {
			t.Errorf("%q accepted", login)
		}
	}
}

func TestIssueBody(t *testing.T) {
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPost || r.URL.Path != "/app/installations/42/access_tokens" || r.Header.Get("Authorization") != "Bearer J" ||
			string(body) != `{"permissions":{"actions":"read","checks":"read","contents":"write","pull_requests":"write"}}` {
			t.Errorf("request %s %s %q", r.Method, r.URL, body)
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"token":"ghs_secret","expires_at":"2026-10-17T13:00:00Z","permissions":{}}`)
	})
	token, expires, err := c.Issue(context.Background(), "J", 42)
	if err != nil || token != "ghs_secret" || !expires.Equal(now.Add(time.Hour)) {
		t.Fatalf("%q %v %v", token, expires, err)
	}
}

func TestIssueFailures(t *testing.T) {
	for name, h := range map[string]http.HandlerFunc{
		"status": func(w http.ResponseWriter, _ *http.Request) { http.Error(w, "ghs_leak", http.StatusForbidden) },
		"no token": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"expires_at":"2026-10-17T13:00:00Z"}`)
		},
		"bad expiry": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"token":"ghs_leak","expires_at":"soon"}`)
		},
	} {
		_, _, err := client(t, h).Issue(context.Background(), "J", 1)
		if err == nil || strings.Contains(err.Error(), "ghs_leak") {
			t.Errorf("%s: %v", name, err)
		}
	}
	var se *StatusError
	_, _, err := client(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusForbidden) }).Issue(context.Background(), "J", 1)
	if !errors.As(err, &se) || se.Status != http.StatusForbidden || Transport(err) {
		t.Errorf("status error %v", err)
	}
}

func TestRevoke(t *testing.T) {
	for status, ok := range map[int]bool{204: true, 401: true, 404: true, 500: false, 403: false} {
		c := client(t, func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete || r.URL.Path != "/installation/token" || r.Header.Get("Authorization") != "Bearer ghs_old" {
				t.Errorf("request %s %s", r.Method, r.URL)
			}
			w.WriteHeader(status)
		})
		if err := c.Revoke(context.Background(), "ghs_old"); (err == nil) != ok {
			t.Errorf("%d: %v", status, err)
		}
	}
}

func TestTransport(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	srv.Close()
	c := Client{Base: srv.URL, HTTP: srv.Client(), Now: time.Now}
	err := c.Revoke(context.Background(), "ghs_x")
	if err == nil || !Transport(err) || strings.Contains(err.Error(), "ghs_x") {
		t.Fatalf("%v", err)
	}
	if Transport(&StatusError{Status: 500}) || Transport(nil) {
		t.Fatal("status counted as transport")
	}
	if New().Base != "https://api.github.com" || New().HTTP.Timeout != 30*time.Second {
		t.Fatal(strconv.Quote(New().Base))
	}
}
