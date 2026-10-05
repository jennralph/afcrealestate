package api

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"strings"
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

// Catalog is the server list plus live load from node heartbeats. Nodes can
// be added at runtime through enrollment, in which case the list is written
// back to the file it was loaded from.
type Catalog struct {
	mu      sync.RWMutex
	servers []ServerConfig
	beats   map[string]heartbeat
	path    string // empty = not persisted
	now     func() time.Time
}

// OfflineAfter is how long a node may stay silent before it is hidden.
const OfflineAfter = 90 * time.Second

// LoadCatalog reads and validates servers.json. A missing file is an empty
// catalog that nodes can enroll into.
func LoadCatalog(path string) (*Catalog, error) {
	var f struct {
		Servers []ServerConfig `json:"servers"`
	}
	data, err := os.ReadFile(path)
	switch {
	case errors.Is(err, os.ErrNotExist):
	case err != nil:
		return nil, err
	default:
		if err := json.Unmarshal(data, &f); err != nil {
			return nil, err
		}
	}
	c, err := NewCatalog(f.Servers)
	if err != nil {
		return nil, err
	}
	c.path = path
	return c, nil
}

// NewCatalog validates servers and returns a catalog for them.
func NewCatalog(servers []ServerConfig) (*Catalog, error) {
	seen := map[string]bool{}
	for i := range servers {
		s := &servers[i]
		if s.ID == "" || seen[s.ID] {
			return nil, fmt.Errorf("server %d: missing or duplicate id", i)
		}
		seen[s.ID] = true
		if err := validate(s); err != nil {
			return nil, fmt.Errorf("server %s: %w", s.ID, err)
		}
	}
	return &Catalog{servers: servers, beats: map[string]heartbeat{}, now: time.Now}, nil
}

var countryCode = regexp.MustCompile(`^[a-z]{2}$`)

// validate checks and normalises one server entry in place.
func validate(s *ServerConfig) error {
	key, err := wgkey.Parse(s.PublicKey)
	if err != nil {
		return err
	}
	s.PublicKey = key
	if len(s.NodeTokenSHA256) != 64 {
		return errors.New("node_token_sha256 must be 64 hex chars")
	}
	if _, err := hex.DecodeString(s.NodeTokenSHA256); err != nil {
		return errors.New("node_token_sha256 must be hex")
	}
	s.CountryCode = strings.ToLower(s.CountryCode)
	if !countryCode.MatchString(s.CountryCode) {
		return errors.New("country_code must be two letters")
	}
	if a, err := netip.ParseAddr(s.IPv4); err != nil || !a.Is4() {
		return errors.New("ipv4 must be an IPv4 address")
	}
	if s.IPv6 != "" {
		if a, err := netip.ParseAddr(s.IPv6); err != nil || !a.Is6() {
			return errors.New("ipv6 must be an IPv6 address")
		}
	}
	if s.Port == 0 {
		s.Port = 51820
	}
	if s.Port < 1 || s.Port > 65535 {
		return errors.New("port out of range")
	}
	if strings.TrimSpace(s.City) == "" || len(s.City) > 60 || len(s.Country) > 60 {
		return errors.New("city and country must be 1-60 characters")
	}
	if s.Country == "" {
		s.Country = strings.ToUpper(s.CountryCode)
	}
	if s.CapacityPeers == 0 {
		s.CapacityPeers = 500
	}
	return nil
}

// Servers returns a snapshot of the configured servers.
func (c *Catalog) Servers() []ServerConfig {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return append([]ServerConfig(nil), c.servers...)
}

// Enroll adds a node, or updates the existing entry with the same public
// key (a node re-running its setup). It returns the stored entry.
func (c *Catalog) Enroll(s ServerConfig) (ServerConfig, error) {
	if err := validate(&s); err != nil {
		return ServerConfig{}, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	idx := -1
	for i, e := range c.servers {
		if e.PublicKey == s.PublicKey {
			idx = i
			break
		}
	}
	if idx >= 0 {
		s.ID = c.servers[idx].ID
	} else {
		s.ID = c.nextID(s)
	}
	old := append([]ServerConfig(nil), c.servers...)
	if idx >= 0 {
		c.servers[idx] = s
	} else {
		c.servers = append(c.servers, s)
	}
	if err := c.save(); err != nil {
		c.servers = old
		return ServerConfig{}, err
	}
	return s, nil
}

var (
	nonSlug  = regexp.MustCompile(`[^a-z0-9]+`)
	unaccent = strings.NewReplacer("á", "a", "à", "a", "â", "a", "ã", "a", "ä", "a", "å", "a",
		"é", "e", "è", "e", "ê", "e", "ë", "e", "í", "i", "ì", "i", "î", "i", "ï", "i",
		"ó", "o", "ò", "o", "ô", "o", "õ", "o", "ö", "o", "ø", "o", "ú", "u", "ù", "u", "û", "u", "ü", "u",
		"ç", "c", "ñ", "n", "ß", "ss")
)

func (c *Catalog) nextID(s ServerConfig) string {
	city := strings.Trim(nonSlug.ReplaceAllString(unaccent.Replace(strings.ToLower(s.City)), "-"), "-")
	if city == "" {
		city = "node"
	}
	if len(city) > 20 {
		city = city[:20]
	}
	taken := map[string]bool{}
	for _, e := range c.servers {
		taken[e.ID] = true
	}
	for n := 1; ; n++ {
		id := fmt.Sprintf("%s-%s-%02d", s.CountryCode, city, n)
		if !taken[id] {
			return id
		}
	}
}

func (c *Catalog) save() error {
	if c.path == "" {
		return nil
	}
	data, err := json.MarshalIndent(map[string]any{"servers": c.servers}, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(c.path), ".servers-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), c.path)
}

// authenticate returns the server whose node token matches.
func (c *Catalog) authenticate(token string) (ServerConfig, bool) {
	sum := sha256.Sum256([]byte(token))
	got := hex.EncodeToString(sum[:])
	for _, s := range c.Servers() {
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
