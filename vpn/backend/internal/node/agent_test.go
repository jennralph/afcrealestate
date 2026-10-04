package node

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"
)

type fakeIface struct {
	peers   map[string]PeerState
	upserts [][]Peer
	removes [][]string
}

func (f *fakeIface) Peers() (map[string]PeerState, error) {
	out := map[string]PeerState{}
	for k, v := range f.peers {
		out[k] = v
	}
	return out, nil
}

func (f *fakeIface) Configure(up []Peer, rm []string) error {
	f.upserts = append(f.upserts, up)
	f.removes = append(f.removes, rm)
	for _, k := range rm {
		delete(f.peers, k)
	}
	for _, p := range up {
		f.peers[p.PublicKey] = PeerState{AllowedIPs: p.AllowedIPs}
	}
	return nil
}

func TestDiff(t *testing.T) {
	want := []Peer{
		{PublicKey: "a", AllowedIPs: []string{"10.64.0.8/32", "fd::8/128"}},
		{PublicKey: "b", AllowedIPs: []string{"10.64.0.9/32"}},
		{PublicKey: "c", AllowedIPs: []string{"10.64.0.10/32"}},
	}
	have := map[string]PeerState{
		"a": {AllowedIPs: []string{"fd::8/128", "10.64.0.8/32"}}, // same set, other order
		"b": {AllowedIPs: []string{"10.64.0.99/32"}},             // changed
		"z": {},                                                  // revoked
	}
	up, rm := Diff(want, have)
	if len(up) != 2 || up[0].PublicKey != "b" || up[1].PublicKey != "c" {
		t.Fatalf("upsert: %+v", up)
	}
	if !reflect.DeepEqual(rm, []string{"z"}) {
		t.Fatalf("remove: %v", rm)
	}
}

func TestSyncOnceAppliesAndReports(t *testing.T) {
	var beat map[string]float64
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/v1/node/peers":
			json.NewEncoder(w).Encode(map[string]any{"peers": []Peer{{PublicKey: "a", AllowedIPs: []string{"10.64.0.8/32"}}}})
		case "/v1/node/heartbeat":
			json.NewDecoder(r.Body).Decode(&beat)
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer api.Close()

	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	iface := &fakeIface{peers: map[string]PeerState{
		"a":       {AllowedIPs: []string{"10.64.0.8/32"}, LastHandshake: now.Add(-time.Minute)},
		"revoked": {AllowedIPs: []string{"10.64.0.50/32"}, LastHandshake: now.Add(-time.Minute)},
	}}
	ag := &Agent{API: api.URL, Token: "tok", Iface: iface, Now: func() time.Time { return now }}
	if err := ag.SyncOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, ok := iface.peers["a"]; !ok || len(iface.peers) != 1 {
		t.Fatalf("peers after sync: %+v", iface.peers)
	}
	if beat["active_peers"] != 1 {
		t.Fatalf("heartbeat should count only admitted, recently active peers: %+v", beat)
	}
}

func TestScrubIdleEndpoints(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/node/peers" {
			json.NewEncoder(w).Encode(map[string]any{"peers": []Peer{
				{PublicKey: "idle", AllowedIPs: []string{"10.64.0.8/32"}},
				{PublicKey: "busy", AllowedIPs: []string{"10.64.0.9/32"}},
			}})
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer api.Close()

	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	iface := &fakeIface{peers: map[string]PeerState{
		"idle": {AllowedIPs: []string{"10.64.0.8/32"}, HasEndpoint: true, LastHandshake: now.Add(-10 * time.Minute)},
		"busy": {AllowedIPs: []string{"10.64.0.9/32"}, HasEndpoint: true, LastHandshake: now.Add(-30 * time.Second)},
	}}
	ag := &Agent{API: api.URL, Token: "t", Iface: iface, Now: func() time.Time { return now }, ScrubAfter: 5 * time.Minute}
	if err := ag.SyncOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(iface.removes) != 1 || !reflect.DeepEqual(iface.removes[0], []string{"idle"}) {
		t.Fatalf("removes: %v", iface.removes)
	}
	if len(iface.upserts[0]) != 1 || iface.upserts[0][0].PublicKey != "idle" {
		t.Fatalf("idle peer must be re-added: %+v", iface.upserts)
	}
	if iface.peers["idle"].HasEndpoint {
		t.Fatal("endpoint survived scrub")
	}
}
