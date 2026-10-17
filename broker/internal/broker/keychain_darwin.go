package broker

/*
#cgo LDFLAGS: -framework Security -framework CoreFoundation
#cgo CFLAGS: -Wno-deprecated-declarations
#include <Security/Security.h>
#include <CoreFoundation/CoreFoundation.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>

static OSStatus signed_runtime(void) {
    SecCodeRef code = NULL;
    CFDictionaryRef info = NULL;
    OSStatus s = SecCodeCopySelf(kSecCSDefaultFlags, &code);
    if (s == errSecSuccess) s = SecCodeCheckValidity(code, kSecCSStrictValidate, NULL);
    if (s == errSecSuccess) s = SecCodeCopySigningInformation(code, kSecCSSigningInformation, &info);
    uint32_t flags = 0;
    if (s == errSecSuccess) {
        CFNumberRef n = CFDictionaryGetValue(info, kSecCodeInfoFlags);
        if (!n || !CFNumberGetValue(n, kCFNumberSInt32Type, &flags) || !(flags & kSecCodeSignatureRuntime)) s = errSecCSUnsigned;
    }
    if (info) CFRelease(info);
    if (code) CFRelease(code);
    return s;
}
// The login keychain is opened by path so a user-set default keychain cannot redirect the item.
// A NULL account matches every account of the service.
static CFMutableDictionaryRef item_query(const char *keychain_path, const char *service, const char *account, SecKeychainRef *keychain, OSStatus *status) {
    *status = SecKeychainOpen(keychain_path, keychain);
    if (*status != errSecSuccess) return NULL;
    CFMutableDictionaryRef q = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDictionarySetValue(q, kSecClass, kSecClassGenericPassword);
    CFStringRef s = CFStringCreateWithCString(NULL, service, kCFStringEncodingUTF8);
    CFDictionarySetValue(q, kSecAttrService, s);
    CFRelease(s);
    if (account) {
        s = CFStringCreateWithCString(NULL, account, kCFStringEncodingUTF8);
        CFDictionarySetValue(q, kSecAttrAccount, s);
        CFRelease(s);
    }
    CFArrayRef list = CFArrayCreate(NULL, (const void **)keychain, 1, &kCFTypeArrayCallBacks);
    CFDictionarySetValue(q, kSecMatchSearchList, list);
    CFRelease(list);
    return q;
}
static OSStatus item_exists(const char *path, const char *service, const char *account) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnAttributes, kCFBooleanTrue);
    CFTypeRef result = NULL;
    s = SecItemCopyMatching(q, &result);
    if (result) CFRelease(result);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
// Attributes only, so it never prompts; *account is the first item's, malloc'd, empty when it has none.
static OSStatus item_accounts(const char *path, const char *service, long *count, char **account) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, NULL, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnAttributes, kCFBooleanTrue);
    CFDictionarySetValue(q, kSecMatchLimit, kSecMatchLimitAll);
    CFTypeRef result = NULL;
    s = SecItemCopyMatching(q, &result);
    if (s == errSecSuccess) {
        *count = CFArrayGetCount((CFArrayRef)result);
        CFStringRef a = *count ? CFDictionaryGetValue(CFArrayGetValueAtIndex((CFArrayRef)result, 0), kSecAttrAccount) : NULL;
        if (a && CFGetTypeID(a) != CFStringGetTypeID()) a = NULL;
        CFIndex size = a ? CFStringGetMaximumSizeForEncoding(CFStringGetLength(a), kCFStringEncodingUTF8) + 1 : 1;
        *account = calloc(1, size);
        if (!*account) s = errSecAllocate;
        else if (a && !CFStringGetCString(a, *account, size, kCFStringEncodingUTF8)) s = errSecDecode;
    }
    if (result) CFRelease(result);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
static OSStatus item_get(const char *path, const char *service, const char *account, void **bytes, long *length) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnData, kCFBooleanTrue);
    CFTypeRef result = NULL;
    s = SecItemCopyMatching(q, &result);
    if (s == errSecSuccess) {
        *length = CFDataGetLength((CFDataRef)result);
        *bytes = malloc(*length);
        if (!*bytes) s = errSecAllocate;
        else memcpy(*bytes, CFDataGetBytePtr((CFDataRef)result), *length);
    }
    if (result) CFRelease(result);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
// An access whose only trusted application is this executable.
static OSStatus self_access(const char *label, SecAccessRef *access) {
    SecTrustedApplicationRef self = NULL;
    OSStatus s = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (s != errSecSuccess) return s;
    const void *apps[] = {self};
    CFArrayRef list = CFArrayCreate(NULL, apps, 1, &kCFTypeArrayCallBacks);
    CFStringRef description = CFStringCreateWithCString(NULL, label, kCFStringEncodingUTF8);
    s = SecAccessCreate(description, list, access);
    CFRelease(description);
    CFRelease(list);
    CFRelease(self);
    return s;
}
// An existing item is deleted, never updated, so a pre-planted permissive ACL does not survive.
static OSStatus token_put(const char *path, const void *bytes, long length) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, "agent-pm-claude", "setup-token", &kc, &s);
    if (!q) return s;
    s = SecItemDelete(q);
    if (s != errSecSuccess && s != errSecItemNotFound) goto done;
    SecAccessRef access = NULL;
    s = self_access("agent-pm Claude setup-token", &access);
    if (s != errSecSuccess) goto done;
    CFDictionaryRemoveValue(q, kSecMatchSearchList);
    CFDataRef data = CFDataCreate(NULL, bytes, length);
    CFDictionarySetValue(q, kSecUseKeychain, kc);
    CFDictionarySetValue(q, kSecValueData, data);
    CFDictionarySetValue(q, kSecAttrAccess, access);
    s = SecItemAdd(q, NULL);
    CFRelease(data);
    CFRelease(access);
done:
    CFRelease(q);
    CFRelease(kc);
    return s;
}
// Replaces the whole ACL in place; content and attributes stay, and a failed change leaves the item as it was.
static OSStatus item_trust_self(const char *path, const char *service, const char *account, const char *label) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnRef, kCFBooleanTrue);
    CFTypeRef item = NULL;
    SecAccessRef access = NULL;
    s = SecItemCopyMatching(q, &item);
    if (s == errSecSuccess) s = self_access(label, &access);
    if (s == errSecSuccess) s = SecKeychainItemSetAccess((SecKeychainItemRef)item, access);
    if (access) CFRelease(access);
    if (item) CFRelease(item);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
static int hidden_token(char *buffer, size_t capacity) {
    struct termios old, hidden;
    if (tcgetattr(STDIN_FILENO, &old) != 0) return -1;
    hidden = old;
    hidden.c_lflag &= ~(ECHO | ECHONL);
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &hidden) != 0) return -1;
    size_t n = 0;
    char c;
    int result = -1;
    while (read(STDIN_FILENO, &c, 1) == 1) {
        if (c == '\n' || c == '\r') { result = (int)n; break; }
        if (n + 1 >= capacity) break;
        buffer[n++] = c;
    }
    tcsetattr(STDIN_FILENO, TCSAFLUSH, &old);
    return result;
}
*/
import "C"

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

// CheckInstalled refuses credential access unless this is the root-owned, hardened-runtime install.
func CheckInstalled() error {
	path, err := os.Executable()
	if err != nil || path != InstalledPath {
		return fmt.Errorf("credential access requires %s; install: %s", InstalledPath, UpgradeCommand)
	}
	for p := path; ; p = filepath.Dir(p) {
		info, err := os.Lstat(p)
		if err != nil {
			return err
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != 0 || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o022 != 0 {
			return fmt.Errorf("unsafe broker installation: %s", p)
		}
		if p == "/" {
			break
		}
	}
	if s := C.signed_runtime(); s != 0 {
		return fmt.Errorf("broker signature lacks hardened runtime (status %d); install: %s", s, UpgradeCommand)
	}
	return nil
}

// ReadHidden reads one line from the terminal with echo off.
func ReadHidden() ([]byte, error) {
	buf := make([]byte, maxToken+2)
	n := C.hidden_token((*C.char)(unsafe.Pointer(&buf[0])), C.size_t(len(buf)))
	if n < 0 {
		erase(buf)
		return nil, errors.New("read the setup-token from an interactive terminal")
	}
	return buf[:int(n)], nil
}

func status(s C.OSStatus) error {
	switch s {
	case 0:
		return nil
	case C.errSecItemNotFound:
		return ErrNotFound
	}
	return fmt.Errorf("keychain status %d", int(s))
}

const tokenService, tokenAccount = "agent-pm-claude", "setup-token"

// fileKeychain is the keychain file at this path, with no install check.
type fileKeychain string

// cStrings converts s to C strings for one call; free releases them.
func cStrings(s ...string) (ps []*C.char, free func()) {
	for _, v := range s {
		ps = append(ps, C.CString(v))
	}
	return ps, func() {
		for _, p := range ps {
			C.free(unsafe.Pointer(p))
		}
	}
}

func (k fileKeychain) get(service, account string) ([]byte, error) {
	p, free := cStrings(string(k), service, account)
	defer free()
	var data unsafe.Pointer
	var n C.long
	if err := status(C.item_get(p[0], p[1], p[2], &data, &n)); err != nil {
		return nil, err
	}
	defer C.free(data)
	b := C.GoBytes(data, C.int(n))
	C.memset(data, 0, C.size_t(n))
	return b, nil
}

func (k fileKeychain) Exists() error {
	p, free := cStrings(string(k), tokenService, tokenAccount)
	defer free()
	return status(C.item_exists(p[0], p[1], p[2]))
}

func (k fileKeychain) Get() ([]byte, error) { return k.get(tokenService, tokenAccount) }

func (k fileKeychain) Put(token []byte) error {
	if len(token) == 0 {
		return errors.New("empty setup-token")
	}
	p := C.CString(string(k))
	defer C.free(unsafe.Pointer(p))
	return status(C.token_put(p, unsafe.Pointer(&token[0]), C.long(len(token))))
}

func (k fileKeychain) AppAccount(app string) (string, error) {
	p, free := cStrings(string(k), app)
	defer free()
	var n C.long
	var account *C.char
	err := status(C.item_accounts(p[0], p[1], &n, &account))
	defer C.free(unsafe.Pointer(account))
	switch {
	case err != nil:
		return "", err
	case n > 1:
		return "", ErrAmbiguous
	}
	return C.GoString(account), nil
}

func (k fileKeychain) AppKey(app, account string) ([]byte, error) { return k.get(app, account) }

func (k fileKeychain) TrustOnlyBroker(app, account string) error {
	p, free := cStrings(string(k), app, account, "agent-pm GitHub App "+app)
	defer free()
	return status(C.item_trust_self(p[0], p[1], p[2], p[3]))
}

// SystemKeychain is the item in the account's login keychain, reachable only from the hardened install.
type SystemKeychain struct{ Home string }

func (k SystemKeychain) login() fileKeychain {
	return fileKeychain(filepath.Join(k.Home, "Library/Keychains/login.keychain-db"))
}

func (k SystemKeychain) Exists() error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().Exists()
}

func (k SystemKeychain) Get() ([]byte, error) {
	if err := CheckInstalled(); err != nil {
		return nil, err
	}
	return k.login().Get()
}

func (k SystemKeychain) Put(token []byte) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().Put(token)
}

func (k SystemKeychain) AppAccount(app string) (string, error) {
	if err := CheckInstalled(); err != nil {
		return "", err
	}
	return k.login().AppAccount(app)
}

func (k SystemKeychain) AppKey(app, account string) ([]byte, error) {
	if err := CheckInstalled(); err != nil {
		return nil, err
	}
	return k.login().AppKey(app, account)
}

func (k SystemKeychain) TrustOnlyBroker(app, account string) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().TrustOnlyBroker(app, account)
}
