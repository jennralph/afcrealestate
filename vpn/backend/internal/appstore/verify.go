// Package appstore verifies StoreKit 2 signed transactions (JWS) offline.
//
// StoreKit 2 hands the app a JWS whose x5c header carries Apple's certificate
// chain. We check that chain against the Apple Root CA - G3 certificate the
// operator configures (download it from https://www.apple.com/certificateauthority/),
// verify the ES256 signature with the leaf key and then read the payload.
// No call to Apple is needed, and no Apple ID or receipt is stored.
package appstore

import (
	"crypto/ecdsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/asn1"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"strings"
	"time"
)

var (
	oidLeaf         = asn1.ObjectIdentifier{1, 2, 840, 113635, 100, 6, 11, 1}
	oidIntermediate = asn1.ObjectIdentifier{1, 2, 840, 113635, 100, 6, 2, 1}
)

// Transaction is the subset of JWSTransactionDecodedPayload we act on.
type Transaction struct {
	TransactionID         string `json:"transactionId"`
	OriginalTransactionID string `json:"originalTransactionId"`
	BundleID              string `json:"bundleId"`
	ProductID             string `json:"productId"`
	ExpiresDateMS         int64  `json:"expiresDate"`
	RevocationDateMS      int64  `json:"revocationDate"`
	Environment           string `json:"environment"`
	Type                  string `json:"type"`
}

// Expires returns the subscription expiry.
func (t Transaction) Expires() time.Time { return time.UnixMilli(t.ExpiresDateMS) }

// Revoked reports whether Apple refunded or revoked the transaction.
func (t Transaction) Revoked() bool { return t.RevocationDateMS != 0 }

// Verifier checks signed transactions for one app.
type Verifier struct {
	Root         *x509.Certificate
	BundleID     string
	ProductIDs   map[string]bool
	AllowSandbox bool
	Now          func() time.Time
}

// Verify validates jws and returns its decoded payload.
func (v Verifier) Verify(jws string) (Transaction, error) {
	parts := strings.Split(jws, ".")
	if len(parts) != 3 {
		return Transaction{}, errors.New("malformed JWS")
	}
	var header struct {
		Alg string   `json:"alg"`
		X5C []string `json:"x5c"`
	}
	if err := decodeSegment(parts[0], &header); err != nil {
		return Transaction{}, fmt.Errorf("header: %w", err)
	}
	if header.Alg != "ES256" || len(header.X5C) != 3 {
		return Transaction{}, errors.New("unexpected JWS header")
	}
	certs := make([]*x509.Certificate, 3)
	for i, s := range header.X5C {
		der, err := base64.StdEncoding.DecodeString(s)
		if err != nil {
			return Transaction{}, fmt.Errorf("x5c[%d]: %w", i, err)
		}
		if certs[i], err = x509.ParseCertificate(der); err != nil {
			return Transaction{}, fmt.Errorf("x5c[%d]: %w", i, err)
		}
	}
	if !certs[2].Equal(v.Root) {
		return Transaction{}, errors.New("chain does not end in the configured Apple root")
	}
	if !hasExtension(certs[0], oidLeaf) || !hasExtension(certs[1], oidIntermediate) {
		return Transaction{}, errors.New("certificates lack Apple App Store markers")
	}
	now := time.Now
	if v.Now != nil {
		now = v.Now
	}
	roots, inter := x509.NewCertPool(), x509.NewCertPool()
	roots.AddCert(v.Root)
	inter.AddCert(certs[1])
	if _, err := certs[0].Verify(x509.VerifyOptions{
		Roots: roots, Intermediates: inter, CurrentTime: now(),
		KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny},
	}); err != nil {
		return Transaction{}, fmt.Errorf("certificate chain: %w", err)
	}
	pub, ok := certs[0].PublicKey.(*ecdsa.PublicKey)
	if !ok {
		return Transaction{}, errors.New("leaf key is not ECDSA")
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || len(sig) != 64 {
		return Transaction{}, errors.New("malformed signature")
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	r, s := new(big.Int).SetBytes(sig[:32]), new(big.Int).SetBytes(sig[32:])
	if !ecdsa.Verify(pub, digest[:], r, s) {
		return Transaction{}, errors.New("bad signature")
	}

	var tx Transaction
	if err := decodeSegment(parts[1], &tx); err != nil {
		return Transaction{}, fmt.Errorf("payload: %w", err)
	}
	if tx.BundleID != v.BundleID {
		return Transaction{}, errors.New("transaction is for another app")
	}
	if !v.ProductIDs[tx.ProductID] {
		return Transaction{}, errors.New("unknown product")
	}
	if tx.Environment != "Production" && !v.AllowSandbox {
		return Transaction{}, errors.New("sandbox transactions are not accepted")
	}
	if tx.OriginalTransactionID == "" || tx.ExpiresDateMS == 0 {
		return Transaction{}, errors.New("not an auto-renewable subscription")
	}
	return tx, nil
}

func decodeSegment(seg string, v any) error {
	raw, err := base64.RawURLEncoding.DecodeString(seg)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

func hasExtension(c *x509.Certificate, oid asn1.ObjectIdentifier) bool {
	for _, e := range c.Extensions {
		if e.Id.Equal(oid) {
			return true
		}
	}
	return false
}
