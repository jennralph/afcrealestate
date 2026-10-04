// Command harbor-node keeps a WireGuard interface's peers in sync with the
// Harbor control plane. Run it on every VPN server:
//
//	HARBOR_NODE_TOKEN=... harbor-node -api https://api.example.net -iface wg0
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jennralph/afcrealestate/vpn/backend/internal/node"
)

func main() {
	apiURL := flag.String("api", "", "control plane base URL")
	iface := flag.String("iface", "wg0", "WireGuard interface")
	interval := flag.Duration("interval", 10*time.Second, "sync interval")
	scrub := flag.Duration("scrub-after", 5*time.Minute, "forget an idle peer's endpoint this long after its last handshake (0 = never)")
	flag.Parse()
	log.SetFlags(0)

	token := os.Getenv("HARBOR_NODE_TOKEN")
	if *apiURL == "" || token == "" {
		log.Fatal("-api and HARBOR_NODE_TOKEN are required")
	}
	k, err := node.OpenKernel(*iface)
	if err != nil {
		log.Fatalf("open %s: %v", *iface, err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ag := &node.Agent{API: *apiURL, Token: token, Iface: k, ScrubAfter: *scrub}
	log.Printf("harbor-node syncing %s every %s", *iface, *interval)
	ag.Run(ctx, *interval)
}
