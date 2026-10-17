package main

/*
#cgo LDFLAGS: -framework Security -framework CoreFoundation
#cgo CFLAGS: -Wno-deprecated-declarations
#include <Security/Security.h>
#include <CoreFoundation/CoreFoundation.h>
#include <pwd.h>
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
static CFMutableDictionaryRef token_query(void) {
    CFMutableDictionaryRef q = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDictionarySetValue(q, kSecClass, kSecClassGenericPassword);
    CFDictionarySetValue(q, kSecAttrService, CFSTR("agent-pm-claude"));
    CFDictionarySetValue(q, kSecAttrAccount, CFSTR("setup-token"));
    return q;
}
static OSStatus token_get(void **bytes, long *length) {
    CFMutableDictionaryRef q = token_query();
    CFDictionarySetValue(q, kSecReturnData, kCFBooleanTrue);
    CFTypeRef result = NULL;
    OSStatus s = SecItemCopyMatching(q, &result);
    if (s == errSecSuccess) {
        *length = CFDataGetLength((CFDataRef)result);
        *bytes = malloc(*length);
        if (!*bytes) s = errSecAllocate;
        else memcpy(*bytes, CFDataGetBytePtr((CFDataRef)result), *length);
    }
    if (result) CFRelease(result);
    CFRelease(q);
    return s;
}
static OSStatus token_put(const void *bytes, long length) {
    SecTrustedApplicationRef self = NULL;
    SecAccessRef access = NULL;
    OSStatus s = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (s != errSecSuccess) return s;
    const void *apps[] = {self};
    CFArrayRef list = CFArrayCreate(NULL, apps, 1, &kCFTypeArrayCallBacks);
    s = SecAccessCreate(CFSTR("agent-pm Claude setup-token"), list, &access);
    CFRelease(list);
    CFRelease(self);
    if (s != errSecSuccess) return s;
    CFDataRef data = CFDataCreate(NULL, bytes, length);
    CFMutableDictionaryRef q = token_query();
    CFDictionarySetValue(q, kSecValueData, data);
    CFDictionarySetValue(q, kSecAttrAccess, access);
    s = SecItemAdd(q, NULL);
    if (s == errSecDuplicateItem) {
        CFDictionaryRemoveValue(q, kSecValueData);
        CFDictionaryRemoveValue(q, kSecAttrAccess);
        CFMutableDictionaryRef values = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
        CFDictionarySetValue(values, kSecValueData, data);
        CFDictionarySetValue(values, kSecAttrAccess, access);
        s = SecItemUpdate(q, values);
        CFRelease(values);
    }
    CFRelease(q);
    CFRelease(data);
    CFRelease(access);
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

func checkInstalled() error {
	path, err := os.Executable()
	if err != nil || path != installedPath {
		return fmt.Errorf("credential access requires %s; install: %s", installedPath, upgradeCommand)
	}
	for p := path; ; p = filepath.Dir(p) {
		info, err := os.Lstat(p)
		if err != nil {
			return err
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != 0 || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 {
			return fmt.Errorf("unsafe broker installation: %s", p)
		}
		if p == "/" {
			break
		}
	}
	if s := C.signed_runtime(); s != 0 {
		return fmt.Errorf("broker signature requires hardened runtime (status %d); install: %s", s, upgradeCommand)
	}
	return nil
}

func terminalToken() ([]byte, error) {
	buf := make([]byte, 16386)
	n := C.hidden_token((*C.char)(unsafe.Pointer(&buf[0])), C.size_t(len(buf)))
	if n < 0 {
		erase(buf)
		return nil, errors.New("read setup-token from an interactive terminal only")
	}
	return buf[:int(n)], nil
}

func (keychainStore) Get() ([]byte, error) {
	if err := checkInstalled(); err != nil {
		return nil, err
	}
	var p unsafe.Pointer
	var n C.long
	if s := C.token_get(&p, &n); s != 0 {
		return nil, fmt.Errorf("Claude credential unavailable (keychain status %d); run agent-pm-broker login claude", s)
	}
	defer C.free(p)
	b := C.GoBytes(p, C.int(n))
	C.memset(p, 0, C.size_t(n))
	return b, nil
}

func (keychainStore) Put(token []byte) error {
	if err := checkInstalled(); err != nil {
		return err
	}
	if len(token) == 0 {
		return errors.New("empty setup-token")
	}
	if s := C.token_put(unsafe.Pointer(&token[0]), C.long(len(token))); s != 0 {
		return fmt.Errorf("keychain write failed (status %d)", s)
	}
	return nil
}
