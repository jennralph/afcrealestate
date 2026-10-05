package ipam

import "testing"

func TestAddresses(t *testing.T) {
	v4, v6, err := Default.Addresses(0)
	if err != nil || v4.String() != "10.64.0.8" || v6.String() != "fd68:6172:626f:7200::8" {
		t.Fatalf("slot 0: %v %v %v", v4, v6, err)
	}
	v4, _, _ = Default.Addresses(300)
	if v4.String() != "10.64.1.52" {
		t.Fatalf("slot 300: %v", v4)
	}
	last := Default.Capacity() - 1
	v4, _, err = Default.Addresses(last)
	if err != nil || v4.String() != "10.127.255.254" {
		t.Fatalf("last slot: %v %v", v4, err)
	}
	if _, _, err := Default.Addresses(last + 1); err == nil {
		t.Fatal("slot past the pool must fail")
	}
	if g, _ := Default.Gateway(2); g.String() != "10.64.0.2" {
		t.Fatalf("gateway: %v", g)
	}
}
