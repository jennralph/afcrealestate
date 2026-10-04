// Package wgkey validates WireGuard public keys as they arrive over the API.
package wgkey

import (
	"encoding/base64"
	"errors"
)

// ErrInvalid is returned for anything that is not a base64 Curve25519 key.
var ErrInvalid = errors.New("invalid WireGuard public key")

// Parse checks that s is a standard-base64 encoding of exactly 32 bytes and is
// not the all-zero key, and returns it in canonical form.
func Parse(s string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil || len(raw) != 32 {
		return "", ErrInvalid
	}
	zero := true
	for _, b := range raw {
		if b != 0 {
			zero = false
			break
		}
	}
	if zero {
		return "", ErrInvalid
	}
	return base64.StdEncoding.EncodeToString(raw), nil
}
