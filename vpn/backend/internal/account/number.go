// Package account generates and normalises anonymous account numbers.
//
// Like Mullvad, Harbor has no usernames, emails or passwords: an account is a
// random 16-digit number the user writes down. The server never stores the
// number itself, only a keyed hash of it, so a leaked database cannot be used
// to log in.
package account

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"math/big"
	"strings"
)

// Digits is the length of an account number (~53 bits of entropy).
const Digits = 16

// ErrMalformed is returned by Normalize for input that cannot be a number.
var ErrMalformed = errors.New("account number must be 16 digits")

// New returns a fresh uniformly random account number.
func New() (string, error) {
	max := new(big.Int).Exp(big.NewInt(10), big.NewInt(Digits), nil)
	n, err := rand.Int(rand.Reader, max)
	if err != nil {
		return "", err
	}
	s := n.String()
	return strings.Repeat("0", Digits-len(s)) + s, nil
}

// Normalize strips the spaces and dashes people type when copying a number
// ("1234 5678 9012 3456") and validates what remains.
func Normalize(in string) (string, error) {
	var b strings.Builder
	for _, r := range in {
		switch {
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == ' ' || r == '-':
		default:
			return "", ErrMalformed
		}
	}
	if b.Len() != Digits {
		return "", ErrMalformed
	}
	return b.String(), nil
}

// Hasher derives the lookup key stored in place of the account number.
type Hasher struct{ secret []byte }

// NewHasher returns a Hasher keyed with secret, which must stay stable for the
// lifetime of the database.
func NewHasher(secret []byte) Hasher { return Hasher{secret: secret} }

// Hash returns the hex HMAC-SHA256 of a normalised account number.
func (h Hasher) Hash(number string) string {
	m := hmac.New(sha256.New, h.secret)
	m.Write([]byte(number))
	return hex.EncodeToString(m.Sum(nil))
}
