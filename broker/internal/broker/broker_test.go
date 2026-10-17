package broker

import (
	"bytes"
	"encoding/hex"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/ophis/agent-pm/broker/internal/docker"
	"github.com/ophis/agent-pm/broker/internal/protocol"
)

type fakeKeychain struct {
	token          []byte
	exists, getErr error
	put            []byte
	gets           int
}

func (k *fakeKeychain) Exists() error { return k.exists }
func (k *fakeKeychain) Get() ([]byte, error) {
	k.gets++
	return append([]byte(nil), k.token...), k.getErr
}
func (k *fakeKeychain) Put(b []byte) error { k.put = append([]byte(nil), b...); return nil }

type fakeExec struct {
	argv  [][]string
	stdin [][]byte
	err   error
}

func (e *fakeExec) run(argv []string, stdin []byte) error {
	e.argv = append(e.argv, argv)
	e.stdin = append(e.stdin, append([]byte(nil), stdin...))
	return e.err
}

const token = "sk-ant-oat01-SECRET"

func TestLogin(t *testing.T) {
	for _, bad := range []string{"", "has space", "tab\there", "nul\x00", strings.Repeat("a", 16385)} {
		k := &fakeKeychain{}
		var out bytes.Buffer
		err := Login(func() ([]byte, error) { return []byte(bad), nil }, k, &out)
		if err == nil || k.put != nil || strings.Contains(err.Error(), bad) && bad != "" {
			t.Fatalf("%q accepted or echoed: %v", bad, err)
		}
	}
	k := &fakeKeychain{}
	var out bytes.Buffer
	read := []byte(token)
	if err := Login(func() ([]byte, error) { return read, nil }, k, &out); err != nil {
		t.Fatal(err)
	}
	if string(k.put) != token || out.String() != "Claude setup-token saved\n" {
		t.Fatalf("%q %q", k.put, out.String())
	}
	if !bytes.Equal(read, make([]byte, len(read))) {
		t.Fatal("token buffer not erased")
	}
	if err := Login(func() ([]byte, error) { return nil, errors.New("no tty") }, &fakeKeychain{}, &out); err == nil {
		t.Fatal("read error ignored")
	}
}

// fakeApps is one App private-key item whose access TrustApps and TrustPartition change like securityd,
// unless frozen; every AppAccess returns a fresh copy, as a real read does.
type fakeApps struct {
	account, key           string
	accountErr, getErr     error
	access                 *AppAccess
	accessErr, readBackErr error
	appsErr, partitionErr  error
	frozen                 bool
	steps                  []string
	password               []byte
	passwordSeen           string
	got                    []byte
	reads                  int
}

const (
	brokerPartition = "cdhash:0a1b2c"
	security        = "/usr/bin/security"
	keychainPass    = "login-keychain-password"
)

// brokerOnly is the agent-pm-claude item's shape: an owner entry, one decrypt entry with only the broker,
// the partition list with only the broker's partition ID.
func brokerOnly() AppAccess {
	return AppAccess{
		Broker:    InstalledPath,
		Partition: brokerPartition,
		Entries: []ACLEntry{
			{Authorizations: []string{"change_acl"}},
			{Authorizations: []string{"encrypt", "decrypt", "derive", "export_clear", "export_wrapped"}, Apps: []string{InstalledPath}},
			{Authorizations: []string{"partition_id"}, Any: true, Partitions: []string{brokerPartition}},
		},
	}
}

// added is an item as security add-generic-password leaves it.
func added() *AppAccess {
	a := brokerOnly()
	a.Entries[1].Apps = []string{security}
	a.Entries[2].Partitions = []string{"apple-tool:"}
	return &a
}

// halfDone is the item v0.6.18 left on the user's host: read entries changed, partition list not.
func halfDone() *AppAccess {
	a := brokerOnly()
	a.Entries[2].Partitions = []string{"apple-tool:"}
	return &a
}

func (k *fakeApps) AppAccess(app, account string) (AppAccess, error) {
	if k.access == nil {
		a := brokerOnly()
		k.access = &a
	}
	if k.reads++; k.reads > 1 && k.readBackErr != nil {
		return AppAccess{}, k.readBackErr
	}
	a := *k.access
	a.Entries = nil
	for _, e := range k.access.Entries {
		e.Authorizations, e.Apps, e.Partitions = slices.Clone(e.Authorizations), slices.Clone(e.Apps), slices.Clone(e.Partitions)
		a.Entries = append(a.Entries, e)
	}
	return a, k.accessErr
}

func (k *fakeApps) AppAccount(app string) (string, error) { return k.account, k.accountErr }
func (k *fakeApps) AppKey(app, account string) ([]byte, error) {
	if account != k.account {
		return nil, ErrNotFound
	}
	k.got = []byte(k.key)
	return k.got, k.getErr
}

func (k *fakeApps) TrustApps(app, account string) error {
	k.steps = append(k.steps, "apps "+app+" "+account)
	if k.appsErr != nil || k.frozen {
		return k.appsErr
	}
	for i, e := range k.access.Entries {
		if slices.ContainsFunc(e.Authorizations, func(x string) bool { return slices.Contains(readAuthorizations, x) }) {
			k.access.Entries[i].Any, k.access.Entries[i].Apps = false, []string{InstalledPath}
		}
	}
	return nil
}

func (k *fakeApps) TrustPartition(app, account string, password []byte) error {
	k.steps = append(k.steps, "partition "+app+" "+account)
	k.password, k.passwordSeen = password, string(password)
	if k.partitionErr != nil || k.frozen {
		return k.partitionErr
	}
	for i, e := range k.access.Entries {
		if slices.Contains(e.Authorizations, "partition_id") {
			k.access.Entries[i].Partitions = []string{brokerPartition}
		}
	}
	return nil
}

// askOnce is a password reader that counts its calls; it fails with err when set.
type askOnce struct {
	calls int
	err   error
}

func (p *askOnce) read() ([]byte, error) {
	p.calls++
	if p.err != nil {
		return nil, p.err
	}
	return []byte(keychainPass), nil
}

const appKey = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ=="

func TestLoginGitHub(t *testing.T) {
	for name, tc := range map[string]struct {
		access *AppAccess
		steps  []string
		asks   int
	}{
		"added":        {access: added(), steps: []string{"apps", "partition"}, asks: 1},
		"half done":    {access: halfDone(), steps: []string{"partition"}, asks: 1},
		"already done": {steps: nil},
	} {
		k := &fakeApps{account: "5352992", key: appKey, access: tc.access}
		ask := &askOnce{}
		var out bytes.Buffer
		if err := LoginGitHub("agent-pm-engineer", k, ask.read, &out); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		var steps []string
		for _, s := range k.steps {
			steps = append(steps, strings.TrimSuffix(s, " agent-pm-engineer 5352992"))
		}
		if !slices.Equal(steps, tc.steps) || ask.calls != tc.asks {
			t.Fatalf("%s: steps %q, asked %d times", name, k.steps, ask.calls)
		}
		if out.String() != "GitHub App agent-pm-engineer private key (App ID 5352992) now trusts only the broker\n" {
			t.Fatalf("%s: %q", name, out.String())
		}
		if !bytes.Equal(k.got, make([]byte, len(appKey))) {
			t.Fatalf("%s: key buffer not erased", name)
		}
		if tc.asks > 0 && (k.passwordSeen != keychainPass || !bytes.Equal(k.password, make([]byte, len(keychainPass)))) {
			t.Fatalf("%s: partition step got %q, buffer after %q", name, k.passwordSeen, k.password)
		}
		if !reflect.DeepEqual(*k.access, brokerOnly()) {
			t.Fatalf("%s: left %#v", name, *k.access)
		}
		var again bytes.Buffer
		if err := LoginGitHub("agent-pm-engineer", k, ask.read, &again); err != nil || ask.calls != tc.asks || again.String() != out.String() {
			t.Fatalf("%s: rerun %v, asked %d times, %q", name, err, ask.calls, again.String())
		}
	}
	for name, k := range map[string]*fakeApps{
		"missing":     {accountErr: ErrNotFound},
		"ambiguous":   {accountErr: ErrAmbiguous},
		"keychain":    {accountErr: errors.New("keychain status -25308")},
		"not an id":   {account: "setup-token", key: appKey},
		"zero id":     {account: "0123", key: appKey},
		"read failed": {account: "5352992", key: appKey, getErr: errors.New("keychain status -128")},
		"empty":       {account: "5352992"},
		"access":      {account: "5352992", key: appKey, access: added(), accessErr: errors.New("keychain status -25293")},
	} {
		ask := &askOnce{}
		var out bytes.Buffer
		err := LoginGitHub("agent-pm-docs", k, ask.read, &out)
		if err == nil || k.steps != nil || ask.calls != 0 || out.Len() != 0 || strings.Contains(err.Error(), appKey) {
			t.Fatalf("%s: %v steps %q out %q", name, err, k.steps, out.String())
		}
		if k.got != nil && !bytes.Equal(k.got, make([]byte, len(k.got))) {
			t.Fatalf("%s: key buffer not erased", name)
		}
	}
}

func TestLoginGitHubStepFails(t *testing.T) {
	const (
		apps      = "done: its read and export entries trust only the broker"
		partition = `its partition list is ["apple-tool:"], not ["cdhash:0a1b2c"]`
		rerun     = "rerun agent-pm-broker login github agent-pm-docs"
	)
	denied := errors.New("keychain status -25293")
	for name, tc := range map[string]struct {
		k     *fakeApps
		ask   *askOnce
		asks  int
		wants []string
	}{
		"partition after apps": {
			k:     &fakeApps{access: added(), partitionErr: denied},
			asks:  1,
			wants: []string{"agent-pm-docs partly changed: changing its partition list failed: keychain status -25293", apps, "not done: " + partition, rerun},
		},
		"partition on half done": {
			k:     &fakeApps{access: halfDone(), partitionErr: denied},
			asks:  1,
			wants: []string{"agent-pm-docs unchanged: changing its partition list failed", apps, "not done: " + partition, rerun},
		},
		"apps": {
			k:     &fakeApps{access: added(), appsErr: denied},
			wants: []string{"agent-pm-docs unchanged: changing its read and export entries failed", "done: nothing", `entry trusts ["/usr/bin/security"]`, partition, rerun},
		},
		"password read": {
			k:     &fakeApps{access: added()},
			ask:   &askOnce{err: errors.New("needs an interactive terminal")},
			asks:  1,
			wants: []string{"agent-pm-docs partly changed: reading the login keychain password failed: needs an interactive terminal", apps, partition, rerun},
		},
		"read back": {
			k:     &fakeApps{access: added(), partitionErr: denied, readBackErr: errors.New("keychain status -25308")},
			asks:  1,
			wants: []string{"agent-pm-docs: changing its partition list failed: keychain status -25293", "reading its access back failed: keychain status -25308", "unknown", rerun},
		},
	} {
		tc.k.account, tc.k.key = "5352992", appKey
		if tc.ask == nil {
			tc.ask = &askOnce{}
		}
		var out bytes.Buffer
		err := LoginGitHub("agent-pm-docs", tc.k, tc.ask.read, &out)
		if err == nil || out.Len() != 0 || tc.ask.calls != tc.asks {
			t.Fatalf("%s: %v out %q asked %d times", name, err, out.String(), tc.ask.calls)
		}
		for _, want := range tc.wants {
			if !strings.Contains(err.Error(), want) {
				t.Fatalf("%s: %v lacks %q", name, err, want)
			}
		}
		if strings.Contains(err.Error(), keychainPass) || strings.Contains(err.Error(), appKey) {
			t.Fatalf("%s: %v", name, err)
		}
		if tc.k.password != nil && !bytes.Equal(tc.k.password, make([]byte, len(keychainPass))) {
			t.Fatalf("%s: password buffer not erased", name)
		}
	}
}

// A step whose error comes with the change applied passes on the access read back.
func TestLoginGitHubStepErrorButDone(t *testing.T) {
	k := &fakeApps{account: "5352992", key: appKey, access: added()}
	var out bytes.Buffer
	err := LoginGitHub("agent-pm-docs", &appliedThenFailed{k}, (&askOnce{}).read, &out)
	if err != nil || out.String() != "GitHub App agent-pm-docs private key (App ID 5352992) now trusts only the broker\n" {
		t.Fatalf("%v %q", err, out.String())
	}
}

type appliedThenFailed struct{ *fakeApps }

func (k *appliedThenFailed) TrustPartition(app, account string, password []byte) error {
	if err := k.fakeApps.TrustPartition(app, account, password); err != nil {
		return err
	}
	return errors.New("keychain status -25293")
}

// Steps that report success but leave the access as it was must not pass for success.
func TestLoginGitHubVerifiesAccess(t *testing.T) {
	with := func(change func(*AppAccess)) *AppAccess {
		a := brokerOnly()
		change(&a)
		return &a
	}
	decrypt := []string{"decrypt"}
	for name, tc := range map[string]struct {
		access *AppAccess
		want   string
	}{
		"observed": {access: with(func(a *AppAccess) {
			a.Entries = []ACLEntry{
				{Authorizations: []string{"change_acl"}},
				{Authorizations: decrypt, Apps: []string{security}},
				{Authorizations: decrypt, Apps: []string{InstalledPath}},
				{Authorizations: []string{"partition_id"}, Any: true, Partitions: []string{"apple-tool:"}},
			}
		}), want: security},
		"security beside broker": {access: with(func(a *AppAccess) { a.Entries[1].Apps = []string{InstalledPath, security} }), want: security},
		"any application":        {access: with(func(a *AppAccess) { a.Entries[1] = ACLEntry{Authorizations: decrypt, Any: true} }), want: "any application"},
		"any authorization": {access: with(func(a *AppAccess) {
			a.Entries = append(a.Entries, ACLEntry{Authorizations: []string{"any"}, Apps: []string{security}})
		}), want: security},
		"export to another app": {access: with(func(a *AppAccess) {
			a.Entries = append(a.Entries, ACLEntry{Authorizations: []string{"export_clear"}, Apps: []string{security}})
		}), want: security},
		"no application": {access: with(func(a *AppAccess) { a.Entries[1].Apps = nil }), want: "no application"},
		"no decrypt entry": {access: with(func(a *AppAccess) {
			a.Entries = []ACLEntry{a.Entries[0], a.Entries[2]}
		}), want: "no entry lets the broker read"},
		"apple-tool partition": {access: with(func(a *AppAccess) { a.Entries[2].Partitions = []string{"apple-tool:"} }), want: "apple-tool:"},
		"extra partition": {access: with(func(a *AppAccess) {
			a.Entries[2].Partitions = []string{brokerPartition, "apple:"}
		}), want: "apple:"},
		"no partition list": {access: with(func(a *AppAccess) { a.Entries = a.Entries[:2] }), want: "partition lists"},
		"two partition lists": {access: with(func(a *AppAccess) {
			a.Entries = append(a.Entries, a.Entries[2])
		}), want: "partition lists"},
	} {
		k := &fakeApps{account: "5352992", key: appKey, access: tc.access, frozen: true}
		var out bytes.Buffer
		err := LoginGitHub("agent-pm-docs", k, (&askOnce{}).read, &out)
		if err == nil || out.Len() != 0 || strings.Contains(err.Error(), appKey) {
			t.Fatalf("%s: %v out %q", name, err, out.String())
		}
		for _, want := range []string{"keychain item agent-pm-docs unchanged", tc.want, "rerun agent-pm-broker login github agent-pm-docs"} {
			if !strings.Contains(err.Error(), want) {
				t.Fatalf("%s: %v lacks %q", name, err, want)
			}
		}
	}
}

func TestParseAccess(t *testing.T) {
	h := func(s string) string { return hex.EncodeToString([]byte(s)) }
	text := strings.Join([]string{
		"self " + h(InstalledPath) + " " + h(brokerPartition),
		"acl " + h("change_acl"),
		"acl " + h("encrypt") + " " + h("decrypt") + " " + h("derive") + " " + h("export_clear") + " " + h("export_wrapped"),
		"app " + h(InstalledPath),
		"acl " + h("partition_id"),
		"any",
		"partition " + h(brokerPartition),
		"",
	}, "\n")
	got, err := parseAccess([]byte(text))
	if err != nil || !reflect.DeepEqual(got, brokerOnly()) {
		t.Fatalf("%v\n%#v", err, got)
	}
	for _, bad := range []string{
		"",
		"acl " + h("decrypt"),
		"self " + h(InstalledPath),
		"self " + h(InstalledPath) + " " + h(brokerPartition) + "\nself " + h(InstalledPath) + " " + h(brokerPartition),
		"self " + h(InstalledPath) + " " + h(brokerPartition) + "\napp " + h(InstalledPath),
		"self " + h(InstalledPath) + " " + h(brokerPartition) + "\nacl zz",
		"self " + h(InstalledPath) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\nany extra",
		"self " + h(InstalledPath) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\nowner " + h("x"),
		"self " + h(InstalledPath) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\napp " + h("a") + " " + h("b"),
	} {
		if _, err := parseAccess([]byte(bad)); err == nil {
			t.Fatalf("%q accepted", bad)
		}
	}
}

func TestReady(t *testing.T) {
	for _, tc := range []struct {
		err  error
		code protocol.Code
	}{
		{nil, ""},
		{ErrNotFound, protocol.LoginRequired},
		{errors.New("user denied"), protocol.KeychainFailed},
	} {
		k := &fakeKeychain{exists: tc.err}
		err := Credentials{Keychain: k}.Ready()
		var e *protocol.Error
		if tc.code == "" && err != nil || tc.code != "" && (!errors.As(err, &e) || e.Code != tc.code) {
			t.Fatalf("%v → %v", tc.err, err)
		}
		if tc.code == protocol.LoginRequired && !strings.Contains(e.Message, "agent-pm-broker login claude") {
			t.Fatal(e.Message)
		}
		if k.gets != 0 {
			t.Fatal("Ready read the token")
		}
		if e != nil && e.Cause != "" {
			t.Fatalf("keychain detail in cause: %q", e.Cause)
		}
	}
}

func TestInject(t *testing.T) {
	e := &fakeExec{}
	k := &fakeKeychain{token: []byte(token)}
	if err := (Credentials{Keychain: k}).Inject(e.run); err != nil {
		t.Fatal(err)
	}
	want := []string{"/bin/sh", "-c", "umask 0377 && set -C && cat > " + docker.SecretsDir + "/claude"}
	if !reflect.DeepEqual(e.argv, [][]string{want}) || string(e.stdin[0]) != token {
		t.Fatalf("%q %q", e.argv, e.stdin)
	}
	for _, tc := range []struct {
		dockerErr, keyErr error
		unreachable       bool
	}{
		{&docker.CommandError{Exit: 1, Stderr: "cat: " + token}, nil, false},
		{errors.Join(docker.ErrUnreachable, errors.New(token)), nil, true},
	} {
		e := &fakeExec{err: tc.dockerErr}
		err := Credentials{Keychain: &fakeKeychain{token: []byte(token), getErr: tc.keyErr}}.Inject(e.run)
		if err == nil || strings.Contains(err.Error(), token) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable {
			t.Fatalf("%v", err)
		}
		var ce *docker.CommandError
		if errors.As(err, &ce) {
			t.Fatal("Docker stderr kept")
		}
	}
}

func TestInjectKeychainFailure(t *testing.T) {
	for keyErr, code := range map[error]protocol.Code{errors.New("locked " + token): protocol.KeychainFailed, ErrNotFound: protocol.LoginRequired} {
		e := &fakeExec{}
		err := Credentials{Keychain: &fakeKeychain{getErr: keyErr}}.Inject(e.run)
		var pe *protocol.Error
		if !errors.As(err, &pe) || pe.Code != code || pe.Cause != "" || strings.Contains(err.Error(), token) || len(e.argv) != 0 {
			t.Fatalf("%v: %v %q", keyErr, err, e.argv)
		}
	}
}
