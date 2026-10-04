package appstore

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/base64"
	"encoding/json"
	"math/big"
	"strings"
	"testing"
	"time"
)

type chain struct {
	root, inter, leaf *x509.Certificate
	leafKey           *ecdsa.PrivateKey
}

func mkChain(t *testing.T, markers bool) chain {
	t.Helper()
	now := time.Now()
	mk := func(tmpl *x509.Certificate, parent *x509.Certificate, pub *ecdsa.PublicKey, signer *ecdsa.PrivateKey) *x509.Certificate {
		der, err := x509.CreateCertificate(rand.Reader, tmpl, parent, pub, signer)
		if err != nil {
			t.Fatal(err)
		}
		c, _ := x509.ParseCertificate(der)
		return c
	}
	rk, _ := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	ik, _ := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	lk, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)

	rootT := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Test Root"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour * 24 * 365), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	root := mk(rootT, rootT, &rk.PublicKey, rk)

	interT := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "Test WWDR"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour * 24 * 365), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	leafT := &x509.Certificate{SerialNumber: big.NewInt(3), Subject: pkix.Name{CommonName: "Test StoreKit"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour * 24 * 365), KeyUsage: x509.KeyUsageDigitalSignature}
	if markers {
		null, _ := asn1.Marshal(asn1.NullRawValue)
		interT.ExtraExtensions = []pkix.Extension{{Id: oidIntermediate, Value: null}}
		leafT.ExtraExtensions = []pkix.Extension{{Id: oidLeaf, Value: null}}
	}
	inter := mk(interT, root, &ik.PublicKey, rk)
	leaf := mk(leafT, inter, &lk.PublicKey, ik)
	return chain{root: root, inter: inter, leaf: leaf, leafKey: lk}
}

func sign(t *testing.T, c chain, payload any) string {
	t.Helper()
	enc := base64.RawURLEncoding
	h, _ := json.Marshal(map[string]any{"alg": "ES256", "x5c": []string{
		base64.StdEncoding.EncodeToString(c.leaf.Raw),
		base64.StdEncoding.EncodeToString(c.inter.Raw),
		base64.StdEncoding.EncodeToString(c.root.Raw),
	}})
	p, _ := json.Marshal(payload)
	signing := enc.EncodeToString(h) + "." + enc.EncodeToString(p)
	d := sha256.Sum256([]byte(signing))
	r, s, err := ecdsa.Sign(rand.Reader, c.leafKey, d[:])
	if err != nil {
		t.Fatal(err)
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return signing + "." + enc.EncodeToString(sig)
}

func goodPayload() map[string]any {
	return map[string]any{
		"transactionId": "2000000001", "originalTransactionId": "2000000000",
		"bundleId": "net.harborvpn.app", "productId": "harbor.monthly",
		"expiresDate": time.Now().Add(30 * 24 * time.Hour).UnixMilli(), "environment": "Production",
		"type": "Auto-Renewable Subscription",
	}
}

func verifier(root *x509.Certificate) Verifier {
	return Verifier{Root: root, BundleID: "net.harborvpn.app", ProductIDs: map[string]bool{"harbor.monthly": true}}
}

func TestVerifyAccepts(t *testing.T) {
	c := mkChain(t, true)
	tx, err := verifier(c.root).Verify(sign(t, c, goodPayload()))
	if err != nil {
		t.Fatal(err)
	}
	if tx.OriginalTransactionID != "2000000000" || tx.Revoked() {
		t.Fatalf("unexpected %+v", tx)
	}
}

func TestVerifyRejects(t *testing.T) {
	c := mkChain(t, true)
	other := mkChain(t, true)
	noMarkers := mkChain(t, false)

	tamper := func(jws string) string {
		parts := strings.Split(jws, ".")
		p := goodPayload()
		p["expiresDate"] = time.Now().Add(1000 * 24 * time.Hour).UnixMilli()
		b, _ := json.Marshal(p)
		parts[1] = base64.RawURLEncoding.EncodeToString(b)
		return strings.Join(parts, ".")
	}
	withField := func(k string, v any) map[string]any {
		p := goodPayload()
		p[k] = v
		return p
	}

	cases := map[string]struct {
		v   Verifier
		jws string
	}{
		"wrong root":      {verifier(other.root), sign(t, c, goodPayload())},
		"no apple OIDs":   {verifier(noMarkers.root), sign(t, noMarkers, goodPayload())},
		"tampered":        {verifier(c.root), tamper(sign(t, c, goodPayload()))},
		"other bundle":    {verifier(c.root), sign(t, c, withField("bundleId", "com.evil"))},
		"unknown product": {verifier(c.root), sign(t, c, withField("productId", "x"))},
		"sandbox":         {verifier(c.root), sign(t, c, withField("environment", "Sandbox"))},
		"not a sub":       {verifier(c.root), sign(t, c, withField("expiresDate", 0))},
		"garbage":         {verifier(c.root), "a.b"},
	}
	for name, tc := range cases {
		if _, err := tc.v.Verify(tc.jws); err == nil {
			t.Errorf("%s: expected rejection", name)
		}
	}
}
