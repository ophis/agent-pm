package broker

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"
)

type probeBody struct {
	t      *testing.T
	reads  int
	closes int
}

func (b *probeBody) Read([]byte) (int, error) {
	b.reads++
	b.t.Fatal("provider probe read the response body")
	return 0, errors.New("unexpected body read")
}

func (b *probeBody) Close() error {
	b.closes++
	return nil
}

func TestOpencodeProbe(t *testing.T) {
	for _, status := range []int{200, 204, 301, 401, 500} {
		k := &fakeSecrets{items: map[string][]byte{"agent-pm-opencode/strata": []byte(apiKey)}}
		calls := 0
		body := &probeBody{t: t}
		transport := roundTrip(func(req *http.Request) (*http.Response, error) {
			calls++
			deadline, ok := req.Context().Deadline()
			if remaining := time.Until(deadline); !ok || remaining <= 0 || remaining > 5*time.Second {
				t.Fatalf("probe deadline: %v %v", deadline, ok)
			}
			if req.URL.String() != "https://strata.test/v1/models" || req.Header.Get("Authorization") != "Bearer "+apiKey {
				t.Fatal("wrong endpoint or authentication")
			}
			response := &http.Response{StatusCode: status, Header: make(http.Header), Body: body}
			response.Header.Set("Location", "https://elsewhere.test/steal")
			return response, nil
		})
		available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "https://strata.test/v1/", transport)
		if err != nil || available != (status >= 200 && status < 300) || calls != 1 {
			t.Fatalf("status=%d available=%v err=%v calls=%d", status, available, err, calls)
		}
		if body.reads != 0 || body.closes != 1 {
			t.Fatalf("status=%d body reads=%d closes=%d", status, body.reads, body.closes)
		}
		for _, key := range k.returned {
			if !bytes.Equal(key, make([]byte, len(key))) {
				t.Fatal("key not erased")
			}
		}
	}
}

func TestOpencodeProbeFailuresAreOpaque(t *testing.T) {
	k := &fakeSecrets{items: map[string][]byte{"agent-pm-opencode/strata": []byte(apiKey)}}
	transport := roundTrip(func(*http.Request) (*http.Response, error) { return nil, errors.New(apiKey) })
	available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "http://strata.test/v1", transport)
	if available || err != nil {
		t.Fatalf("%v %v", available, err)
	}
	for _, failure := range []error{ErrNotFound, ErrNeedsDialog, errors.New(apiKey)} {
		k.readErr = failure
		available, err = (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", "http://strata.test/v1", transport)
		if available || err == nil || strings.Contains(err.Error(), apiKey) {
			t.Fatalf("%v %v", available, err)
		}
	}
}

func TestOpencodeProbeRejectsUntrustedInputsBeforeReadingKeys(t *testing.T) {
	for _, base := range []string{"file:///tmp/key", "https://u:secret@host/v1", "https://host/v1?key=x", "https://host/v1#x", "http://", "http://host/\n", "http://host:bad/v1"} {
		k := &fakeSecrets{}
		available, err := (OpencodeKeys{Keychain: k}).Probe(context.Background(), "strata", base, nil)
		if available || err == nil || len(k.returned) != 0 {
			t.Fatalf("accepted %q", base)
		}
	}
	if ValidOpencodeURL("https://host/v1") != true {
		t.Fatal("valid URL refused")
	}
	if _, err := (OpencodeKeys{Keychain: &fakeSecrets{}}).Probe(context.Background(), "../bad", "https://host/v1", nil); err == nil {
		t.Fatal("bad provider accepted")
	}
}
