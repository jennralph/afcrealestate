// Command harbor-api serves the Harbor VPN control plane.
//
//	HARBOR_ACCOUNT_SECRET=$(openssl rand -hex 32) harbor-api \
//	  -listen :8080 -db /var/lib/harbor/db.json -servers /etc/harbor/servers.json \
//	  -apple-root /etc/harbor/AppleRootCA-G3.cer -bundle-id net.harborvpn.app \
//	  -products harbor.monthly,harbor.yearly
//
// Put it behind a TLS-terminating proxy (Caddy, nginx) and pass -trust-proxy.
// The proxy must not write access logs either.
package main

import (
	"context"
	"crypto/x509"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jennralph/afcrealestate/vpn/backend/internal/account"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/api"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/appstore"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/ipam"
	"github.com/jennralph/afcrealestate/vpn/backend/internal/store"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8080", "address to serve HTTP on")
	dbPath := flag.String("db", "harbor-db.json", "account database file")
	serversPath := flag.String("servers", "servers.json", "server catalog")
	appleRoot := flag.String("apple-root", "", "Apple Root CA - G3 certificate (DER); empty disables purchases")
	bundleID := flag.String("bundle-id", "net.harborvpn.app", "iOS bundle identifier")
	products := flag.String("products", "harbor.monthly,harbor.yearly", "comma-separated subscription product IDs")
	sandbox := flag.Bool("allow-sandbox", false, "accept StoreKit sandbox/TestFlight transactions")
	trustProxy := flag.Bool("trust-proxy", false, "use X-Forwarded-For for rate limiting")
	flag.Parse()
	log.SetFlags(0) // journald adds timestamps; we add nothing else

	secret := os.Getenv("HARBOR_ACCOUNT_SECRET")
	if len(secret) < 32 {
		log.Fatal("HARBOR_ACCOUNT_SECRET must be set to at least 32 characters (openssl rand -hex 32)")
	}
	st, err := store.Open(*dbPath, ipam.Default.Capacity())
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	cat, err := api.LoadCatalog(*serversPath)
	if err != nil {
		log.Fatalf("load servers: %v", err)
	}
	srv := &api.Server{
		Store: st, Catalog: cat, Hasher: account.NewHasher([]byte(secret)),
		Pools: ipam.Default, TrustProxy: *trustProxy,
	}
	if *appleRoot != "" {
		der, err := os.ReadFile(*appleRoot)
		if err != nil {
			log.Fatalf("apple root: %v", err)
		}
		root, err := x509.ParseCertificate(der)
		if err != nil {
			log.Fatalf("apple root: %v", err)
		}
		ids := map[string]bool{}
		for _, p := range strings.Split(*products, ",") {
			ids[strings.TrimSpace(p)] = true
		}
		srv.Verifier = appstore.Verifier{Root: root, BundleID: *bundleID, ProductIDs: ids, AllowSandbox: *sandbox}
	}

	hs := &http.Server{
		Addr: *listen, Handler: srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second,
		ErrorLog: log.New(discard{}, "", 0), // TLS/handshake noise would include client addresses
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = hs.Shutdown(shutdown)
	}()
	log.Printf("harbor-api listening on %s", *listen)
	if err := hs.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

type discard struct{}

func (discard) Write(p []byte) (int, error) { return len(p), nil }
