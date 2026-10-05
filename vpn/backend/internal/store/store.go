// Package store keeps accounts and devices.
//
// The data set is deliberately tiny — a keyed hash per account, its paid-until
// date and its devices' public keys — so it is held in memory and snapshotted
// atomically to a JSON file after each change. Nothing about traffic,
// connection times or client IP addresses is ever recorded here.
package store

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

var (
	ErrNotFound      = errors.New("not found")
	ErrExists        = errors.New("already exists")
	ErrDeviceLimit   = errors.New("device limit reached")
	ErrKeyInUse      = errors.New("public key already registered")
	ErrPoolExhausted = errors.New("address pool exhausted")
	ErrClosed        = errors.New("account limit reached")
)

// Device is one WireGuard key belonging to an account.
type Device struct {
	ID         string    `json:"id"`
	Name       string    `json:"name"`
	PublicKey  string    `json:"public_key"`
	Slot       int       `json:"slot"`
	Created    time.Time `json:"created"`
	KeyRotated time.Time `json:"key_rotated"`
}

// Account is identified only by the keyed hash of its number.
type Account struct {
	Hash      string    `json:"hash"`
	Created   time.Time `json:"created"` // day precision only
	PaidUntil time.Time `json:"paid_until"`
	Devices   []Device  `json:"devices"`
	// Owner marks the first account on the server: whoever set it up.
	Owner bool `json:"owner,omitempty"`
}

// Paid reports whether the account has an active subscription at t.
func (a Account) Paid(t time.Time) bool { return a.PaidUntil.After(t) }

type snapshot struct {
	Accounts     map[string]*Account `json:"accounts"`
	Transactions map[string]string   `json:"transactions"` // original transaction id -> account hash
	NextSlot     int                 `json:"next_slot"`
	FreeSlots    []int               `json:"free_slots"`
}

// Store is safe for concurrent use.
type Store struct {
	mu       sync.RWMutex
	path     string // empty = memory only
	capacity int
	s        snapshot
	keys     map[string]string // public key -> account hash
	now      func() time.Time
}

// Open loads path (creating it on first save) with room for capacity slots.
// An empty path keeps everything in memory, which tests use.
func Open(path string, capacity int) (*Store, error) {
	st := &Store{
		path:     path,
		capacity: capacity,
		s: snapshot{
			Accounts:     map[string]*Account{},
			Transactions: map[string]string{},
		},
		keys: map[string]string{},
		now:  time.Now,
	}
	if path != "" {
		data, err := os.ReadFile(path)
		switch {
		case errors.Is(err, os.ErrNotExist):
		case err != nil:
			return nil, err
		default:
			if err := json.Unmarshal(data, &st.s); err != nil {
				return nil, err
			}
			if st.s.Accounts == nil {
				st.s.Accounts = map[string]*Account{}
			}
			if st.s.Transactions == nil {
				st.s.Transactions = map[string]string{}
			}
		}
	}
	for h, a := range st.s.Accounts {
		for _, d := range a.Devices {
			st.keys[d.PublicKey] = h
		}
	}
	return st, nil
}

// SetClock overrides the time source (tests).
func (st *Store) SetClock(now func() time.Time) { st.now = now }

func (st *Store) save() error {
	if st.path == "" {
		return nil
	}
	data, err := json.Marshal(st.s)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(st.path), ".store-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), st.path)
}

// CreateAccount registers a new account hash.
func (st *Store) CreateAccount(hash string) (Account, error) {
	a, _, err := st.CreateAccountCapped(hash, 0, 0)
	return a, err
}

// CreateAccountCapped is CreateAccount for servers that limit sign-ups:
// it refuses once maxAccounts exist (0 = no limit), and gives the very
// first account on the server ownerDays of paid access, so whoever set the
// server up gets every location and device slot without the App Store.
func (st *Store) CreateAccountCapped(hash string, maxAccounts, ownerDays int) (Account, bool, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if _, ok := st.s.Accounts[hash]; ok {
		return Account{}, false, ErrExists
	}
	if maxAccounts > 0 && len(st.s.Accounts) >= maxAccounts {
		return Account{}, false, ErrClosed
	}
	first := len(st.s.Accounts) == 0
	y, m, d := st.now().UTC().Date()
	a := &Account{Hash: hash, Created: time.Date(y, m, d, 0, 0, 0, 0, time.UTC), Devices: []Device{}}
	a.Owner = first
	if first && ownerDays > 0 {
		a.PaidUntil = st.now().UTC().AddDate(0, 0, ownerDays)
	}
	st.s.Accounts[hash] = a
	if err := st.save(); err != nil {
		delete(st.s.Accounts, hash)
		return Account{}, false, err
	}
	return clone(a), first, nil
}

// Account returns a copy of the account with the given hash.
func (st *Store) Account(hash string) (Account, error) {
	st.mu.RLock()
	defer st.mu.RUnlock()
	a, ok := st.s.Accounts[hash]
	if !ok {
		return Account{}, ErrNotFound
	}
	return clone(a), nil
}

// AddDevice registers pubkey for the account, honouring maxDevices.
func (st *Store) AddDevice(hash, pubkey, name string, maxDevices int) (Device, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	a, ok := st.s.Accounts[hash]
	if !ok {
		return Device{}, ErrNotFound
	}
	if owner, used := st.keys[pubkey]; used {
		if owner == hash {
			for _, d := range a.Devices {
				if d.PublicKey == pubkey {
					return d, nil // idempotent retry after a lost response
				}
			}
		}
		return Device{}, ErrKeyInUse
	}
	if len(a.Devices) >= maxDevices {
		return Device{}, ErrDeviceLimit
	}
	slot, err := st.takeSlot()
	if err != nil {
		return Device{}, err
	}
	now := st.now().UTC()
	d := Device{ID: newID(), Name: name, PublicKey: pubkey, Slot: slot, Created: now, KeyRotated: now}
	a.Devices = append(a.Devices, d)
	st.keys[pubkey] = hash
	if err := st.save(); err != nil {
		a.Devices = a.Devices[:len(a.Devices)-1]
		delete(st.keys, pubkey)
		st.releaseSlot(slot)
		return Device{}, err
	}
	return d, nil
}

// RotateKey replaces a device's public key, keeping its addresses.
func (st *Store) RotateKey(hash, id, pubkey string) (Device, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	a, ok := st.s.Accounts[hash]
	if !ok {
		return Device{}, ErrNotFound
	}
	if _, used := st.keys[pubkey]; used {
		return Device{}, ErrKeyInUse
	}
	for i := range a.Devices {
		if a.Devices[i].ID != id {
			continue
		}
		old := a.Devices[i]
		a.Devices[i].PublicKey = pubkey
		a.Devices[i].KeyRotated = st.now().UTC()
		delete(st.keys, old.PublicKey)
		st.keys[pubkey] = hash
		if err := st.save(); err != nil {
			a.Devices[i] = old
			delete(st.keys, pubkey)
			st.keys[old.PublicKey] = hash
			return Device{}, err
		}
		return a.Devices[i], nil
	}
	return Device{}, ErrNotFound
}

// RemoveDevice deletes a device and frees its addresses.
func (st *Store) RemoveDevice(hash, id string) error {
	st.mu.Lock()
	defer st.mu.Unlock()
	a, ok := st.s.Accounts[hash]
	if !ok {
		return ErrNotFound
	}
	for i, d := range a.Devices {
		if d.ID != id {
			continue
		}
		a.Devices = append(a.Devices[:i:i], a.Devices[i+1:]...)
		delete(st.keys, d.PublicKey)
		st.releaseSlot(d.Slot)
		return st.save()
	}
	return ErrNotFound
}

// ApplyPurchase extends an account to until for an App Store subscription.
// A subscription follows the account it was most recently restored on, so
// one purchase cannot keep several accounts paid at once.
func (st *Store) ApplyPurchase(hash, originalTxID string, until time.Time) (Account, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	a, ok := st.s.Accounts[hash]
	if !ok {
		return Account{}, ErrNotFound
	}
	if prev, ok := st.s.Transactions[originalTxID]; ok && prev != hash {
		if old, ok := st.s.Accounts[prev]; ok && old.PaidUntil.After(st.now()) {
			old.PaidUntil = st.now().UTC()
		}
	}
	st.s.Transactions[originalTxID] = hash
	if until.After(a.PaidUntil) {
		a.PaidUntil = until.UTC()
	}
	return clone(a), st.save()
}

// Grant extends an account's paid period to until, for operators giving
// access outside the App Store (friends, testers, the web version).
func (st *Store) Grant(hash string, until time.Time) (Account, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	a, ok := st.s.Accounts[hash]
	if !ok {
		return Account{}, ErrNotFound
	}
	if until.After(a.PaidUntil) {
		a.PaidUntil = until.UTC()
	}
	return clone(a), st.save()
}

// Peer is what a VPN node needs to admit a device.
type Peer struct {
	PublicKey string `json:"public_key"`
	Slot      int    `json:"slot"`
}

// Peers lists every device whose account satisfies admit, sorted by key so
// nodes can diff cheaply.
func (st *Store) Peers(admit func(Account) bool) []Peer {
	st.mu.RLock()
	defer st.mu.RUnlock()
	var out []Peer
	for _, a := range st.s.Accounts {
		if !admit(*a) {
			continue
		}
		for _, d := range a.Devices {
			out = append(out, Peer{PublicKey: d.PublicKey, Slot: d.Slot})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].PublicKey < out[j].PublicKey })
	return out
}

func (st *Store) takeSlot() (int, error) {
	if n := len(st.s.FreeSlots); n > 0 {
		slot := st.s.FreeSlots[n-1]
		st.s.FreeSlots = st.s.FreeSlots[:n-1]
		return slot, nil
	}
	if st.s.NextSlot >= st.capacity {
		return 0, ErrPoolExhausted
	}
	st.s.NextSlot++
	return st.s.NextSlot - 1, nil
}

func (st *Store) releaseSlot(slot int) { st.s.FreeSlots = append(st.s.FreeSlots, slot) }

func clone(a *Account) Account {
	c := *a
	c.Devices = append([]Device{}, a.Devices...)
	return c
}

func newID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
