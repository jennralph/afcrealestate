package store

import (
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestPersistsAndReusesSlots(t *testing.T) {
	path := filepath.Join(t.TempDir(), "db.json")
	st, err := Open(path, 10)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.CreateAccount("h1"); err != nil {
		t.Fatal(err)
	}
	if _, err := st.CreateAccount("h1"); !errors.Is(err, ErrExists) {
		t.Fatalf("duplicate: %v", err)
	}
	d1, _ := st.AddDevice("h1", "k1", "a", 5)
	d2, _ := st.AddDevice("h1", "k2", "b", 5)
	if d1.Slot != 0 || d2.Slot != 1 {
		t.Fatalf("slots %d %d", d1.Slot, d2.Slot)
	}
	if err := st.RemoveDevice("h1", d1.ID); err != nil {
		t.Fatal(err)
	}

	re, err := Open(path, 10)
	if err != nil {
		t.Fatal(err)
	}
	a, err := re.Account("h1")
	if err != nil || len(a.Devices) != 1 || a.Devices[0].PublicKey != "k2" {
		t.Fatalf("reloaded %+v %v", a, err)
	}
	if !a.Created.Equal(a.Created.Truncate(24 * time.Hour)) {
		t.Fatalf("creation time should be day precision: %v", a.Created)
	}
	if _, err := re.AddDevice("h1", "k2", "dup", 5); err != nil {
		t.Fatalf("re-adding own key should be idempotent: %v", err)
	}
	d3, _ := re.AddDevice("h1", "k3", "c", 5)
	if d3.Slot != 0 {
		t.Fatalf("freed slot not reused: %d", d3.Slot)
	}
	re.CreateAccount("h2")
	if _, err := re.AddDevice("h2", "k3", "x", 5); !errors.Is(err, ErrKeyInUse) {
		t.Fatalf("key reuse across accounts: %v", err)
	}
}

func TestPoolExhaustion(t *testing.T) {
	st, _ := Open("", 1)
	st.CreateAccount("h")
	if _, err := st.AddDevice("h", "k1", "", 5); err != nil {
		t.Fatal(err)
	}
	if _, err := st.AddDevice("h", "k2", "", 5); !errors.Is(err, ErrPoolExhausted) {
		t.Fatalf("got %v", err)
	}
}

func TestGrantOnlyExtends(t *testing.T) {
	st, _ := Open("", 10)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	st.SetClock(func() time.Time { return now })
	st.CreateAccount("h")
	if _, err := st.Grant("nope", now); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown account: %v", err)
	}
	a, _ := st.Grant("h", now.AddDate(1, 0, 0))
	if !a.Paid(now) {
		t.Fatal("grant should make the account paid")
	}
	b, _ := st.Grant("h", now.AddDate(0, 1, 0))
	if !b.PaidUntil.Equal(a.PaidUntil) {
		t.Fatal("a shorter grant must not cut an existing one")
	}
}
