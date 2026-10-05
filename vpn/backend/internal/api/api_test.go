package api

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jennralph/afcrealestate/vpn/backend/internal/account"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/appstore"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/ipam"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/store"
)

func randKey(t *testing.T) string {
	t.Helper()
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(b)
}

func tokenHash(tok string) string {
	s := sha256.Sum256([]byte(tok))
	return hex.EncodeToString(s[:])
}

type fakeVerifier struct {
	tx  appstore.Transaction
	err error
}

func (f fakeVerifier) Verify(string) (appstore.Transaction, error) { return f.tx, f.err }

type harness struct {
	t     *testing.T
	srv   *Server
	h     http.Handler
	clock time.Time
}

func newHarness(t *testing.T) *harness {
	st, err := store.Open("", ipam.Default.Capacity())
	if err != nil {
		t.Fatal(err)
	}
	cat, err := NewCatalog([]ServerConfig{
		{ID: "se-sto-01", CountryCode: "se", Country: "Sweden", City: "Stockholm", IPv4: "198.51.100.1", PublicKey: randKey(t), Free: true, CapacityPeers: 100, NodeTokenSHA256: tokenHash("free-node")},
		{ID: "us-nyc-01", CountryCode: "us", Country: "United States", City: "New York", IPv4: "198.51.100.2", PublicKey: randKey(t), CapacityPeers: 100, LinkMbps: 1000, NodeTokenSHA256: tokenHash("paid-node")},
	})
	if err != nil {
		t.Fatal(err)
	}
	hs := &harness{t: t, clock: time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)}
	now := func() time.Time { return hs.clock }
	st.SetClock(now)
	cat.now = now
	hs.srv = &Server{Store: st, Catalog: cat, Hasher: account.NewHasher([]byte("test")), Pools: ipam.Default, Now: now}
	hs.h = hs.srv.Handler()
	return hs
}

func (hs *harness) do(method, path, auth string, body any, out any) int {
	hs.t.Helper()
	var rd *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	} else {
		rd = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rd)
	req.RemoteAddr = "192.0.2.10:5555"
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	rec := httptest.NewRecorder()
	hs.h.ServeHTTP(rec, req)
	if out != nil && rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), out); err != nil {
			hs.t.Fatalf("%s %s: decode %q: %v", method, path, rec.Body.String(), err)
		}
	}
	return rec.Code
}

func TestAccountDeviceLifecycle(t *testing.T) {
	hs := newHarness(t)

	var acct accountJSON
	if code := hs.do("POST", "/v1/accounts", "", nil, &acct); code != http.StatusCreated {
		t.Fatalf("create: %d", code)
	}
	if len(acct.Number) != 16 || acct.Tier != "free" || acct.MaxDevices != FreeDevices {
		t.Fatalf("unexpected account %+v", acct)
	}
	// People paste numbers with spaces.
	auth := "Account " + acct.Number[:4] + " " + acct.Number[4:8] + " " + acct.Number[8:12] + " " + acct.Number[12:]

	var dev deviceJSON
	key := randKey(t)
	if code := hs.do("POST", "/v1/devices", auth, map[string]string{"public_key": key, "name": "Jenn's iPhone"}, &dev); code != http.StatusCreated {
		t.Fatalf("add device: %d", code)
	}
	if dev.IPv4 != "10.64.0.8/32" || dev.IPv6 != "fd68:6172:626f:7200::8/128" {
		t.Fatalf("addresses: %s %s", dev.IPv4, dev.IPv6)
	}

	// Retrying the same registration is idempotent.
	var again deviceJSON
	if code := hs.do("POST", "/v1/devices", auth, map[string]string{"public_key": key}, &again); code != http.StatusCreated || again.ID != dev.ID {
		t.Fatalf("retry: %d %+v", code, again)
	}

	// The free tier allows one device.
	var e map[string]string
	if code := hs.do("POST", "/v1/devices", auth, map[string]string{"public_key": randKey(t)}, &e); code != http.StatusConflict || e["code"] != "device_limit" {
		t.Fatalf("limit: %d %v", code, e)
	}

	// Key rotation keeps the address.
	var rotated deviceJSON
	newKey := randKey(t)
	if code := hs.do("PUT", "/v1/devices/"+dev.ID+"/key", auth, map[string]string{"public_key": newKey}, &rotated); code != http.StatusOK {
		t.Fatalf("rotate: %d", code)
	}
	if rotated.PublicKey != newKey || rotated.IPv4 != dev.IPv4 {
		t.Fatalf("rotate result %+v", rotated)
	}

	if code := hs.do("DELETE", "/v1/devices/"+dev.ID, auth, nil, nil); code != http.StatusNoContent {
		t.Fatalf("delete: %d", code)
	}
	var got accountJSON
	hs.do("GET", "/v1/account", auth, nil, &got)
	if len(got.Devices) != 0 {
		t.Fatalf("devices after delete: %+v", got.Devices)
	}
}

func TestAuthRejectsBadNumbers(t *testing.T) {
	hs := newHarness(t)
	for _, auth := range []string{"", "Account 123", "Account 0000000000000000", "Bearer 1234567812345678"} {
		if code := hs.do("GET", "/v1/account", auth, nil, nil); code != http.StatusUnauthorized {
			t.Errorf("%q: got %d", auth, code)
		}
	}
}

func TestInvalidKeyRejected(t *testing.T) {
	hs := newHarness(t)
	var acct accountJSON
	hs.do("POST", "/v1/accounts", "", nil, &acct)
	for _, k := range []string{"", "abc", base64.StdEncoding.EncodeToString(make([]byte, 32))} {
		if code := hs.do("POST", "/v1/devices", "Account "+acct.Number, map[string]string{"public_key": k}, nil); code != http.StatusBadRequest {
			t.Errorf("key %q: got %d", k, code)
		}
	}
}

func TestSignupRateLimit(t *testing.T) {
	hs := newHarness(t)
	for i := range 5 {
		if code := hs.do("POST", "/v1/accounts", "", nil, nil); code != http.StatusCreated {
			t.Fatalf("signup %d: %d", i, code)
		}
	}
	if code := hs.do("POST", "/v1/accounts", "", nil, nil); code != http.StatusTooManyRequests {
		t.Fatalf("6th signup: %d", code)
	}
	hs.clock = hs.clock.Add(13 * time.Minute) // one token refills every 12 minutes
	if code := hs.do("POST", "/v1/accounts", "", nil, nil); code != http.StatusCreated {
		t.Fatalf("after refill: %d", code)
	}
}

func TestServersHideOfflineAndReportLoad(t *testing.T) {
	hs := newHarness(t)
	var list struct {
		Servers []serverJSON `json:"servers"`
		DNS     dnsJSON      `json:"dns"`
	}
	hs.do("GET", "/v1/servers", "", nil, &list)
	if len(list.Servers) != 0 {
		t.Fatalf("servers without heartbeat should be hidden: %+v", list.Servers)
	}

	if code := hs.do("POST", "/v1/node/heartbeat", "Bearer paid-node", map[string]any{"active_peers": 10, "mbps": 400}, nil); code != http.StatusNoContent {
		t.Fatalf("heartbeat: %d", code)
	}
	if code := hs.do("POST", "/v1/node/heartbeat", "Bearer wrong", map[string]any{"active_peers": 1}, nil); code != http.StatusUnauthorized {
		t.Fatalf("bad token heartbeat: %d", code)
	}
	hs.do("GET", "/v1/servers", "", nil, &list)
	if len(list.Servers) != 1 || list.Servers[0].ID != "us-nyc-01" || list.Servers[0].Load != 40 {
		t.Fatalf("servers: %+v", list.Servers)
	}
	if list.DNS.BlockAds[0] != "10.64.0.2" || list.DNS.BlockMalware[0] != "10.64.0.3" {
		t.Fatalf("dns: %+v", list.DNS)
	}

	hs.clock = hs.clock.Add(OfflineAfter + time.Second)
	hs.do("GET", "/v1/servers", "", nil, &list)
	if len(list.Servers) != 0 {
		t.Fatalf("stale server still listed")
	}
}

func TestNodePeersRespectTier(t *testing.T) {
	hs := newHarness(t)
	var acct accountJSON
	hs.do("POST", "/v1/accounts", "", nil, &acct)
	auth := "Account " + acct.Number
	key := randKey(t)
	hs.do("POST", "/v1/devices", auth, map[string]string{"public_key": key}, nil)

	peers := func(token string) []nodePeer {
		var out struct {
			Peers []nodePeer `json:"peers"`
		}
		if code := hs.do("GET", "/v1/node/peers", "Bearer "+token, nil, &out); code != http.StatusOK {
			t.Fatalf("peers: %d", code)
		}
		return out.Peers
	}
	if p := peers("free-node"); len(p) != 1 || p[0].PublicKey != key || len(p[0].AllowedIPs) != 2 {
		t.Fatalf("free node peers: %+v", p)
	}
	if p := peers("paid-node"); len(p) != 0 {
		t.Fatalf("free account leaked onto paid node: %+v", p)
	}

	// A purchase admits the device to paid nodes and raises the device limit.
	hs.srv.Verifier = fakeVerifier{tx: appstore.Transaction{OriginalTransactionID: "tx1", ExpiresDateMS: hs.clock.Add(30 * 24 * time.Hour).UnixMilli()}}
	var paid accountJSON
	if code := hs.do("POST", "/v1/purchases/appstore", auth, map[string]string{"signed_transaction": "x"}, &paid); code != http.StatusOK {
		t.Fatalf("purchase: %d", code)
	}
	if paid.Tier != "paid" || paid.MaxDevices != PaidDevices {
		t.Fatalf("after purchase: %+v", paid)
	}
	if p := peers("paid-node"); len(p) != 1 {
		t.Fatalf("paid node peers: %+v", p)
	}

	// Expiry drops the device from paid nodes again.
	hs.clock = hs.clock.Add(31 * 24 * time.Hour)
	if p := peers("paid-node"); len(p) != 0 {
		t.Fatalf("expired account still on paid node")
	}
}

func TestPurchaseMovesBetweenAccounts(t *testing.T) {
	hs := newHarness(t)
	var a1, a2 accountJSON
	hs.do("POST", "/v1/accounts", "", nil, &a1)
	hs.do("POST", "/v1/accounts", "", nil, &a2)
	hs.srv.Verifier = fakeVerifier{tx: appstore.Transaction{OriginalTransactionID: "tx1", ExpiresDateMS: hs.clock.Add(time.Hour * 24).UnixMilli()}}
	hs.do("POST", "/v1/purchases/appstore", "Account "+a1.Number, map[string]string{"signed_transaction": "x"}, nil)
	hs.do("POST", "/v1/purchases/appstore", "Account "+a2.Number, map[string]string{"signed_transaction": "x"}, nil)

	var g1, g2 accountJSON
	hs.do("GET", "/v1/account", "Account "+a1.Number, nil, &g1)
	hs.do("GET", "/v1/account", "Account "+a2.Number, nil, &g2)
	if g1.Tier != "free" || g2.Tier != "paid" {
		t.Fatalf("subscription should follow the latest account: %s / %s", g1.Tier, g2.Tier)
	}
}

func TestPurchaseRejected(t *testing.T) {
	hs := newHarness(t)
	var a accountJSON
	hs.do("POST", "/v1/accounts", "", nil, &a)
	if code := hs.do("POST", "/v1/purchases/appstore", "Account "+a.Number, map[string]string{"signed_transaction": "x"}, nil); code != http.StatusNotImplemented {
		t.Fatalf("no verifier: %d", code)
	}
	hs.srv.Verifier = fakeVerifier{err: errors.New("bad signature")}
	if code := hs.do("POST", "/v1/purchases/appstore", "Account "+a.Number, map[string]string{"signed_transaction": "x"}, nil); code != http.StatusBadRequest {
		t.Fatalf("bad tx: %d", code)
	}
}

func TestEnrollAddsAndUpdatesNodes(t *testing.T) {
	hs := newHarness(t)
	node := map[string]any{
		"country_code": "BR", "country": "Brazil", "city": "São Paulo", "ipv4": "198.51.100.77",
		"public_key": randKey(t), "free": true, "node_token_sha256": tokenHash("br-node"),
	}
	if code := hs.do("POST", "/v1/node/enroll", "Bearer anything", node, nil); code != http.StatusUnauthorized {
		t.Fatalf("enrollment must be off without a secret: %d", code)
	}
	hs.srv.EnrollSecret = "s3cret-s3cret-s3cret-s3cret-s3cret"
	if code := hs.do("POST", "/v1/node/enroll", "Bearer wrong", node, nil); code != http.StatusUnauthorized {
		t.Fatalf("wrong secret: %d", code)
	}
	var got map[string]string
	if code := hs.do("POST", "/v1/node/enroll", "Bearer "+hs.srv.EnrollSecret, node, &got); code != http.StatusOK || got["id"] != "br-sao-paulo-01" {
		t.Fatalf("enroll: %d %v", code, got)
	}
	// Re-running setup on the same node updates it instead of duplicating.
	node["city"] = "Sao Paulo"
	hs.do("POST", "/v1/node/enroll", "Bearer "+hs.srv.EnrollSecret, node, &got)
	if n := len(hs.srv.Catalog.Servers()); n != 3 || got["id"] != "br-sao-paulo-01" {
		t.Fatalf("re-enroll: %d servers, id %v", n, got)
	}
	// The new node can authenticate and shows up once it heartbeats.
	hs.do("POST", "/v1/node/heartbeat", "Bearer br-node", map[string]any{"active_peers": 1}, nil)
	var list struct {
		Servers []serverJSON `json:"servers"`
	}
	hs.do("GET", "/v1/servers", "", nil, &list)
	if len(list.Servers) != 1 || list.Servers[0].City != "Sao Paulo" || list.Servers[0].CountryCode != "br" {
		t.Fatalf("servers: %+v", list.Servers)
	}
	node["ipv4"] = "not-an-ip"
	if code := hs.do("POST", "/v1/node/enroll", "Bearer "+hs.srv.EnrollSecret, node, nil); code != http.StatusBadRequest {
		t.Fatalf("bad node accepted: %d", code)
	}
}

func TestPrivateServerSignups(t *testing.T) {
	hs := newHarness(t)
	hs.srv.MaxAccounts, hs.srv.OwnerDays = 2, 3650
	var owner, second accountJSON
	hs.do("POST", "/v1/accounts", "", nil, &owner)
	hs.do("POST", "/v1/accounts", "", nil, &second)
	if owner.Tier != "paid" || owner.MaxDevices != PaidDevices || second.Tier != "free" {
		t.Fatalf("owner %s / second %s", owner.Tier, second.Tier)
	}
	var e map[string]string
	if code := hs.do("POST", "/v1/accounts", "", nil, &e); code != http.StatusForbidden || e["code"] != "signups_closed" {
		t.Fatalf("third signup: %d %v", code, e)
	}
	if !owner.Owner || second.Owner {
		t.Fatalf("owner flags: %v %v", owner.Owner, second.Owner)
	}
	hs.srv.EnrollSecret = "s3cret-s3cret-s3cret-s3cret-s3cret"
	var sec map[string]string
	if code := hs.do("GET", "/v1/owner/enroll", "Account "+second.Number, nil, nil); code != http.StatusForbidden {
		t.Fatalf("non-owner got the enroll secret: %d", code)
	}
	if code := hs.do("GET", "/v1/owner/enroll", "Account "+owner.Number, nil, &sec); code != http.StatusOK || sec["secret"] != hs.srv.EnrollSecret {
		t.Fatalf("owner enroll: %d %v", code, sec)
	}
}
