// Package node is the agent that runs on each VPN server. It keeps the
// WireGuard interface's peer list in step with the control plane and reports
// load. It writes nothing to disk.
package node

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"slices"
	"time"
)

// Peer is a peer as the control plane describes it.
type Peer struct {
	PublicKey  string   `json:"public_key"`
	AllowedIPs []string `json:"allowed_ips"`
}

// PeerState is a peer as the kernel reports it.
type PeerState struct {
	AllowedIPs    []string
	HasEndpoint   bool
	LastHandshake time.Time
	RxBytes       int64
	TxBytes       int64
}

// Interface is the WireGuard device the agent manages.
type Interface interface {
	Peers() (map[string]PeerState, error)
	// Configure removes then upserts peers in one operation.
	Configure(upsert []Peer, remove []string) error
}

// Agent syncs one interface against the API.
type Agent struct {
	API    string // e.g. https://api.example.net
	Token  string
	Iface  Interface
	Client *http.Client
	Now    func() time.Time

	// ScrubAfter is how long after its last handshake an idle peer is
	// removed and re-added, which wipes its remembered endpoint (the client's
	// real IP) and handshake time from kernel memory. Zero disables it.
	ScrubAfter time.Duration

	lastBytes int64
	lastAt    time.Time
}

// ActiveWindow is how recent a handshake must be to count a peer as online.
// WireGuard re-handshakes every two minutes on an active tunnel.
const ActiveWindow = 3 * time.Minute

// Run syncs every interval until ctx ends.
func (a *Agent) Run(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		if err := a.SyncOnce(ctx); err != nil {
			log.Printf("sync: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// SyncOnce fetches the desired peers, applies the difference and reports load.
func (a *Agent) SyncOnce(ctx context.Context) error {
	want, err := a.fetchPeers(ctx)
	if err != nil {
		return err
	}
	have, err := a.Iface.Peers()
	if err != nil {
		return err
	}
	upsert, remove := Diff(want, have)
	// Load counts only peers that are still admitted, measured before
	// Configure touches the interface.
	kept := map[string]PeerState{}
	for _, p := range want {
		if st, ok := have[p.PublicKey]; ok {
			kept[p.PublicKey] = st
		}
	}

	now := a.now()
	if a.ScrubAfter > 0 {
		wantByKey := map[string]Peer{}
		for _, p := range want {
			wantByKey[p.PublicKey] = p
		}
		var scrub []string
		for k, st := range have {
			p, keep := wantByKey[k]
			if keep && st.HasEndpoint && !st.LastHandshake.IsZero() && now.Sub(st.LastHandshake) > a.ScrubAfter {
				scrub = append(scrub, k)
				if !slices.ContainsFunc(upsert, func(u Peer) bool { return u.PublicKey == k }) {
					upsert = append(upsert, p)
				}
			}
		}
		remove = append(remove, scrub...)
	}
	if len(upsert) > 0 || len(remove) > 0 {
		if err := a.Iface.Configure(upsert, remove); err != nil {
			return fmt.Errorf("configure: %w", err)
		}
	}
	return a.heartbeat(ctx, kept, now)
}

// Diff returns peers to add or update and keys to remove.
func Diff(want []Peer, have map[string]PeerState) (upsert []Peer, remove []string) {
	seen := map[string]bool{}
	for _, p := range want {
		seen[p.PublicKey] = true
		cur, ok := have[p.PublicKey]
		if !ok || !sameSet(cur.AllowedIPs, p.AllowedIPs) {
			upsert = append(upsert, p)
		}
	}
	for k := range have {
		if !seen[k] {
			remove = append(remove, k)
		}
	}
	slices.Sort(remove)
	return upsert, remove
}

func sameSet(a, b []string) bool {
	a, b = slices.Clone(a), slices.Clone(b)
	slices.Sort(a)
	slices.Sort(b)
	return slices.Equal(a, b)
}

func (a *Agent) fetchPeers(ctx context.Context) ([]Peer, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, a.API+"/v1/node/peers", nil)
	req.Header.Set("Authorization", "Bearer "+a.Token)
	resp, err := a.client().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("peers: HTTP %d", resp.StatusCode)
	}
	var body struct {
		Peers []Peer `json:"peers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	return body.Peers, nil
}

func (a *Agent) heartbeat(ctx context.Context, have map[string]PeerState, now time.Time) error {
	active := 0
	var total int64
	for _, st := range have {
		if !st.LastHandshake.IsZero() && now.Sub(st.LastHandshake) < ActiveWindow {
			active++
		}
		total += st.RxBytes + st.TxBytes
	}
	var mbps float64
	if !a.lastAt.IsZero() && total >= a.lastBytes {
		if secs := now.Sub(a.lastAt).Seconds(); secs > 0 {
			mbps = float64(total-a.lastBytes) * 8 / 1e6 / secs
		}
	}
	a.lastBytes, a.lastAt = total, now

	payload, _ := json.Marshal(map[string]any{"active_peers": active, "mbps": mbps})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, a.API+"/v1/node/heartbeat", bytes.NewReader(payload))
	req.Header.Set("Authorization", "Bearer "+a.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client().Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("heartbeat: HTTP %d", resp.StatusCode)
	}
	return nil
}

func (a *Agent) client() *http.Client {
	if a.Client != nil {
		return a.Client
	}
	return &http.Client{Timeout: 15 * time.Second}
}

func (a *Agent) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}
