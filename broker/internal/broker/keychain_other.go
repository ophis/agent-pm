//go:build !darwin

package broker

import "errors"

var errPlatform = errors.New("the broker's keychain and terminal access require macOS")

func CheckInstalled() error       { return errPlatform }
func ReadHidden() ([]byte, error) { return nil, errPlatform }

type SystemKeychain struct{ Home string }

func (SystemKeychain) Exists() error        { return errPlatform }
func (SystemKeychain) Get() ([]byte, error) { return nil, errPlatform }
func (SystemKeychain) Put([]byte) error     { return errPlatform }
