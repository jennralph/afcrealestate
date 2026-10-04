package api

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/jennralph/afcrealestate/vpn/backend/internal/wgkey"
)

// ServerConfig is one VPN node as the operator describes it in servers.json.
type ServerConfig struct {
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
	Features    []string `json:"features,omitempty"`
	// CapacityPeers and LinkMbps turn heartbeats into a load percentage.
	CapacityPeers int `json:"capacity_peers"`
	LinkMbps      int `json:"link_mbps"`
	// NodeTokenSHA256 is the hex SHA-256 of the secret the node agent sends.
	NodeTokenSHA256 string `json:"node_token_sha256"`
}

type heartbeat struct {
	at          time.Time
	activePeers int
	mbps        float64
}

// Catalog is the server list plus live load from node heartbeats.
type Catalog struct {
	servers []ServerConfig
	mu      sync.RWMutex
	beats   map[string]heartbeat
	now     func() time.Time
}

// OfflineAfter is how long a node may stay silent before it is hidden.
const OfflineAfter = 90 * time.Second

// LoadCatalog reads and validates servers.json.
func LoadCatalog(path string) (*Catalog, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var f struct {
		Servers []ServerConfig `json:"servers"`
	}
	if err := json.Unmarshal(data, &f); err != nil {
		return nil, err
	}
	return NewCatalog(f.Servers)
}

// NewCatalog validates servers and returns a catalog for them.
func NewCatalog(servers []ServerConfig) (*Catalog, error) {
	seen := map[string]bool{}
	for i, s := range servers {
		if s.ID == "" || seen[s.ID] {
			return nil, fmt.Errorf("server %d: missing or duplicate id", i)
		}
		seen[s.ID] = true
		if _, err := wgkey.Parse(s.PublicKey); err != nil {
			return nil, fmt.Errorf("server %s: %w", s.ID, err)
		}
		if len(s.NodeTokenSHA256) != 64 {
			return nil, fmt.Errorf("server %s: node_token_sha256 must be 64 hex chars", s.ID)
		}
		if s.Port == 0 {
			servers[i].Port = 51820
		}
	}
	return &Catalog{servers: servers, beats: map[string]heartbeat{}, now: time.Now}, nil
}

// authenticate returns the server whose node token matches.
func (c *Catalog) authenticate(token string) (ServerConfig, bool) {
	sum := sha256.Sum256([]byte(token))
	got := hex.EncodeToString(sum[:])
	for _, s := range c.servers {
		if subtle.ConstantTimeCompare([]byte(got), []byte(s.NodeTokenSHA256)) == 1 {
			return s, true
		}
	}
	return ServerConfig{}, false
}

func (c *Catalog) record(id string, activePeers int, mbps float64) {
	c.mu.Lock()
	c.beats[id] = heartbeat{at: c.now(), activePeers: activePeers, mbps: mbps}
	c.mu.Unlock()
}

// status returns whether a node is online and its load in percent.
func (c *Catalog) status(s ServerConfig) (online bool, load int) {
	c.mu.RLock()
	b, ok := c.beats[s.ID]
	c.mu.RUnlock()
	if !ok || c.now().Sub(b.at) > OfflineAfter {
		return false, 0
	}
	var frac float64
	if s.CapacityPeers > 0 {
		frac = float64(b.activePeers) / float64(s.CapacityPeers)
	}
	if s.LinkMbps > 0 {
		frac = max(frac, b.mbps/float64(s.LinkMbps))
	}
	return true, int(min(frac, 1) * 100)
}
