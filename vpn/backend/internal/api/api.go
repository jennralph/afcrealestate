// Package api is Harbor's control plane: anonymous accounts, device keys,
// the server list, App Store purchases, and the peer feed VPN nodes sync from.
//
// It never logs requests, and it never stores client IP addresses; the only
// IP-derived state is an in-memory rate limiter for account creation.
package api

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jennralph/afcrealestate/vpn/backend/internal/account"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/appstore"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/ipam"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/store"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/wgkey"
)

// Device limits per tier.
const (
	FreeDevices = 1
	PaidDevices = 7
)

// PurchaseVerifier is satisfied by appstore.Verifier.
type PurchaseVerifier interface {
	Verify(jws string) (appstore.Transaction, error)
}

// Server wires the handlers together.
type Server struct {
	Store    *store.Store
	Catalog  *Catalog
	Hasher   account.Hasher
	Pools    ipam.Pools
	Verifier PurchaseVerifier // nil disables purchases
	// TrustProxy uses the first X-Forwarded-For hop for rate limiting; enable
	// only behind a reverse proxy that sets it.
	TrustProxy bool
	// MaxAccounts closes sign-ups once this many accounts exist (0 = open).
	MaxAccounts int
	// OwnerDays gives the first account created on the server this many
	// days of paid access: the person who set up a private server.
	OwnerDays int
	// EnrollSecret lets new VPN nodes add themselves to the catalog
	// (POST /v1/node/enroll). Empty disables enrollment.
	EnrollSecret string
	Now          func() time.Time
	// Web, if set, serves the web app at "/".
	Web http.Handler

	signups *limiter
}

// Handler returns the HTTP handler for the API.
func (s *Server) Handler() http.Handler {
	if s.Now == nil {
		s.Now = time.Now
	}
	if s.signups == nil {
		// 5 new accounts per address per hour, refilled gradually.
		s.signups = newLimiter(5, time.Hour/5, s.Now)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/accounts", s.createAccount)
	mux.HandleFunc("GET /v1/account", s.withAccount(s.getAccount))
	mux.HandleFunc("POST /v1/devices", s.withAccount(s.addDevice))
	mux.HandleFunc("PUT /v1/devices/{id}/key", s.withAccount(s.rotateKey))
	mux.HandleFunc("DELETE /v1/devices/{id}", s.withAccount(s.removeDevice))
	mux.HandleFunc("POST /v1/purchases/appstore", s.withAccount(s.applyPurchase))
	mux.HandleFunc("GET /v1/servers", s.listServers)
	mux.HandleFunc("GET /v1/node/peers", s.withNode(s.nodePeers))
	mux.HandleFunc("POST /v1/node/heartbeat", s.withNode(s.nodeHeartbeat))
	mux.HandleFunc("POST /v1/node/enroll", s.enrollNode)
	mux.HandleFunc("GET /v1/owner/enroll", s.withAccount(s.ownerEnroll))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	if s.Web != nil {
		mux.Handle("GET /", s.Web)
	}
	return limitBody(mux)
}

// ---- accounts & devices ----------------------------------------------------

type deviceJSON struct {
	ID         string    `json:"id"`
	Name       string    `json:"name"`
	PublicKey  string    `json:"public_key"`
	IPv4       string    `json:"ipv4_address"`
	IPv6       string    `json:"ipv6_address"`
	Created    time.Time `json:"created"`
	KeyRotated time.Time `json:"key_rotated"`
}

type accountJSON struct {
	Number     string       `json:"number,omitempty"` // only on creation
	Tier       string       `json:"tier"`
	PaidUntil  *time.Time   `json:"paid_until"`
	MaxDevices int          `json:"max_devices"`
	Owner      bool         `json:"owner,omitempty"`
	Devices    []deviceJSON `json:"devices"`
}

func (s *Server) accountView(a store.Account) accountJSON {
	v := accountJSON{Tier: "free", MaxDevices: FreeDevices, Devices: []deviceJSON{}, Owner: a.Owner}
	if a.Paid(s.Now()) {
		v.Tier, v.MaxDevices = "paid", PaidDevices
	}
	if !a.PaidUntil.IsZero() {
		t := a.PaidUntil
		v.PaidUntil = &t
	}
	for _, d := range a.Devices {
		v.Devices = append(v.Devices, s.deviceView(d))
	}
	return v
}

func (s *Server) deviceView(d store.Device) deviceJSON {
	v4, v6, _ := s.Pools.Addresses(d.Slot)
	return deviceJSON{
		ID: d.ID, Name: d.Name, PublicKey: d.PublicKey,
		IPv4: v4.String() + "/32", IPv6: v6.String() + "/128",
		Created: d.Created, KeyRotated: d.KeyRotated,
	}
}

func (s *Server) createAccount(w http.ResponseWriter, r *http.Request) {
	if !s.signups.allow(s.clientIP(r)) {
		writeError(w, http.StatusTooManyRequests, "too_many_accounts", "Too many new accounts from this network. Try again later.")
		return
	}
	for range 5 { // collisions are astronomically unlikely; retry anyway
		num, err := account.New()
		if err != nil {
			internalError(w, err)
			return
		}
		a, _, err := s.Store.CreateAccountCapped(s.Hasher.Hash(num), s.MaxAccounts, s.OwnerDays)
		if errors.Is(err, store.ErrExists) {
			continue
		}
		if errors.Is(err, store.ErrClosed) {
			writeError(w, http.StatusForbidden, "signups_closed",
				"This Harbor server isn't taking new accounts. If you already have one, sign in with your account number.")
			return
		}
		if err != nil {
			internalError(w, err)
			return
		}
		v := s.accountView(a)
		v.Number = num
		writeJSON(w, http.StatusCreated, v)
		return
	}
	internalError(w, errors.New("could not allocate account number"))
}

type accountHandler func(http.ResponseWriter, *http.Request, store.Account)

// withAccount authenticates "Authorization: Account <number>".
func (s *Server) withAccount(h accountHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		raw, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Account ")
		num, err := account.Normalize(raw)
		if !ok || err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_account", "Enter your 16-digit account number.")
			return
		}
		a, err := s.Store.Account(s.Hasher.Hash(num))
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_account", "That account number doesn't exist.")
			return
		}
		h(w, r, a)
	}
}

func (s *Server) getAccount(w http.ResponseWriter, _ *http.Request, a store.Account) {
	writeJSON(w, http.StatusOK, s.accountView(a))
}

func (s *Server) addDevice(w http.ResponseWriter, r *http.Request, a store.Account) {
	var req struct {
		PublicKey string `json:"public_key"`
		Name      string `json:"name"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	key, err := wgkey.Parse(req.PublicKey)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_key", err.Error())
		return
	}
	name := cleanName(req.Name)
	limit := FreeDevices
	if a.Paid(s.Now()) {
		limit = PaidDevices
	}
	d, err := s.Store.AddDevice(a.Hash, key, name, limit)
	switch {
	case errors.Is(err, store.ErrDeviceLimit):
		writeError(w, http.StatusConflict, "device_limit", fmt.Sprintf("This account already has %d device(s). Remove one to add this iPhone.", limit))
	case errors.Is(err, store.ErrKeyInUse):
		writeError(w, http.StatusConflict, "key_in_use", "This key is already registered.")
	case err != nil:
		internalError(w, err)
	default:
		writeJSON(w, http.StatusCreated, s.deviceView(d))
	}
}

func (s *Server) rotateKey(w http.ResponseWriter, r *http.Request, a store.Account) {
	var req struct {
		PublicKey string `json:"public_key"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	key, err := wgkey.Parse(req.PublicKey)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_key", err.Error())
		return
	}
	d, err := s.Store.RotateKey(a.Hash, r.PathValue("id"), key)
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, "no_device", "This device was removed from your account.")
	case errors.Is(err, store.ErrKeyInUse):
		writeError(w, http.StatusConflict, "key_in_use", "This key is already registered.")
	case err != nil:
		internalError(w, err)
	default:
		writeJSON(w, http.StatusOK, s.deviceView(d))
	}
}

func (s *Server) removeDevice(w http.ResponseWriter, r *http.Request, a store.Account) {
	err := s.Store.RemoveDevice(a.Hash, r.PathValue("id"))
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, "no_device", "No such device.")
	case err != nil:
		internalError(w, err)
	default:
		w.WriteHeader(http.StatusNoContent)
	}
}

func (s *Server) applyPurchase(w http.ResponseWriter, r *http.Request, a store.Account) {
	if s.Verifier == nil {
		writeError(w, http.StatusNotImplemented, "purchases_disabled", "In-app purchases are not configured on this server.")
		return
	}
	var req struct {
		SignedTransaction string `json:"signed_transaction"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	tx, err := s.Verifier.Verify(req.SignedTransaction)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_transaction", err.Error())
		return
	}
	until := tx.Expires()
	if tx.Revoked() {
		until = time.Time{}
	}
	updated, err := s.Store.ApplyPurchase(a.Hash, tx.OriginalTransactionID, until)
	if err != nil {
		internalError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.accountView(updated))
}

// ---- servers ----------------------------------------------------------------

type serverJSON struct {
	ID          string   `json:"id"`
	CountryCode string   `json:"country_code"`
	Country     string   `json:"country"`
	City        string   `json:"city"`
	Latitude    float64  `json:"latitude"`
	Longitude   float64  `json:"longitude"`
	Hostname    string   `json:"hostname"`
	IPv4        string   `json:"ipv4"`
	IPv6        string   `json:"ipv6,omitempty"`
	Port        int      `json:"port"`
	PublicKey   string   `json:"public_key"`
	Free        bool     `json:"free"`
	Features    []string `json:"features"`
	Load        int      `json:"load"`
}

type dnsJSON struct {
	Standard     []string `json:"standard"`
	BlockAds     []string `json:"block_ads"`
	BlockMalware []string `json:"block_ads_malware"`
}

func (s *Server) listServers(w http.ResponseWriter, _ *http.Request) {
	out := struct {
		Servers []serverJSON `json:"servers"`
		DNS     dnsJSON      `json:"dns"`
	}{Servers: []serverJSON{}}
	for _, c := range s.Catalog.Servers() {
		online, load := s.Catalog.status(c)
		if !online {
			continue
		}
		features := c.Features
		if features == nil {
			features = []string{}
		}
		out.Servers = append(out.Servers, serverJSON{
			ID: c.ID, CountryCode: c.CountryCode, Country: c.Country, City: c.City,
			Latitude: c.Latitude, Longitude: c.Longitude, Hostname: c.Hostname,
			IPv4: c.IPv4, IPv6: c.IPv6, Port: c.Port, PublicKey: c.PublicKey,
			Free: c.Free, Features: features, Load: load,
		})
	}
	resolvers := func(n int) []string {
		v4, v6 := s.Pools.Gateway(n)
		return []string{v4.String(), v6.String()}
	}
	out.DNS = dnsJSON{Standard: resolvers(1), BlockAds: resolvers(2), BlockMalware: resolvers(3)}
	w.Header().Set("Cache-Control", "public, max-age=30")
	writeJSON(w, http.StatusOK, out)
}

// ---- node feed ----------------------------------------------------------------

type nodeHandler func(http.ResponseWriter, *http.Request, ServerConfig)

func (s *Server) withNode(h nodeHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tok, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		srv, found := s.Catalog.authenticate(tok)
		if !ok || !found {
			writeError(w, http.StatusUnauthorized, "invalid_node", "unknown node token")
			return
		}
		h(w, r, srv)
	}
}

type nodePeer struct {
	PublicKey  string   `json:"public_key"`
	AllowedIPs []string `json:"allowed_ips"`
}

func (s *Server) nodePeers(w http.ResponseWriter, _ *http.Request, srv ServerConfig) {
	now := s.Now()
	admit := func(a store.Account) bool { return srv.Free || a.Paid(now) }
	peers := []nodePeer{}
	for _, p := range s.Store.Peers(admit) {
		v4, v6, err := s.Pools.Addresses(p.Slot)
		if err != nil {
			continue
		}
		peers = append(peers, nodePeer{PublicKey: p.PublicKey, AllowedIPs: []string{v4.String() + "/32", v6.String() + "/128"}})
	}
	writeJSON(w, http.StatusOK, map[string]any{"peers": peers})
}

func (s *Server) nodeHeartbeat(w http.ResponseWriter, r *http.Request, srv ServerConfig) {
	var req struct {
		ActivePeers int     `json:"active_peers"`
		Mbps        float64 `json:"mbps"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	s.Catalog.record(srv.ID, req.ActivePeers, req.Mbps)
	w.WriteHeader(http.StatusNoContent)
}

// enrollNode lets a freshly installed VPN node add itself to the server
// list, so adding a country is just booting a server with the setup script.
func (s *Server) enrollNode(w http.ResponseWriter, r *http.Request) {
	tok, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if s.EnrollSecret == "" || !ok ||
		subtle.ConstantTimeCompare([]byte(tok), []byte(s.EnrollSecret)) != 1 {
		writeError(w, http.StatusUnauthorized, "invalid_enroll", "enrollment is disabled or the secret is wrong")
		return
	}
	var req ServerConfig
	if !readJSON(w, r, &req) {
		return
	}
	saved, err := s.Catalog.Enroll(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_server", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"id": saved.ID})
}

// ownerEnroll gives the server's owner the enrollment secret, so the web
// app can show a ready-to-paste setup script for adding a location.
func (s *Server) ownerEnroll(w http.ResponseWriter, _ *http.Request, a store.Account) {
	if !a.Owner {
		writeError(w, http.StatusForbidden, "not_owner", "Only the server's owner can add locations.")
		return
	}
	if s.EnrollSecret == "" {
		writeError(w, http.StatusNotFound, "enroll_disabled", "Adding locations isn't enabled on this server.")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]string{"secret": s.EnrollSecret})
}

// ---- helpers ----------------------------------------------------------------

func (s *Server) clientIP(r *http.Request) string {
	if s.TrustProxy {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			first, _, _ := strings.Cut(xff, ",")
			return strings.TrimSpace(first)
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func cleanName(n string) string {
	n = strings.TrimSpace(n)
	if n == "" || !utf8.ValidString(n) {
		return "iPhone"
	}
	if r := []rune(n); len(r) > 40 {
		n = string(r[:40])
	}
	return n
}

func limitBody(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
		h.ServeHTTP(w, r)
	})
}

func readJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "Malformed request body.")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"code": code, "message": msg})
}

func internalError(w http.ResponseWriter, err error) {
	log.Printf("internal error: %v", err) // no request data, by design
	writeError(w, http.StatusInternalServerError, "internal", "Something went wrong on our side. Please try again.")
}
