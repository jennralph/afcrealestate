package api

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEnrollPersists(t *testing.T) {
	path := filepath.Join(t.TempDir(), "servers.json")
	c, err := LoadCatalog(path) // missing file = empty catalog
	if err != nil || len(c.Servers()) != 0 {
		t.Fatalf("load: %v", err)
	}
	if _, err := c.Enroll(ServerConfig{CountryCode: "de", City: "Frankfurt", IPv4: "198.51.100.5",
		PublicKey: randKey(t), NodeTokenSHA256: tokenHash("x")}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
	again, err := LoadCatalog(path)
	if err != nil || len(again.Servers()) != 1 || again.Servers()[0].ID != "de-frankfurt-01" || again.Servers()[0].Country != "DE" {
		t.Fatalf("reload: %+v %v", again.Servers(), err)
	}
}
