//go:build !darwin

package main

import "errors"

func checkInstalled() error {
	return errors.New("credential access requires the installed macOS broker")
}
func terminalToken() ([]byte, error)       { return nil, errors.New("login requires a macOS terminal") }
func (keychainStore) Get() ([]byte, error) { return nil, errors.New("keychain requires macOS") }
func (keychainStore) Put([]byte) error     { return errors.New("keychain requires macOS") }
