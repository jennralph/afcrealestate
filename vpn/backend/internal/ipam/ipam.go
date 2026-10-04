// Package ipam maps device slots to tunnel addresses.
//
// Every device gets one IPv4 and one IPv6 address that is valid on every
// server, so switching location never requires a new registration. Slot n
// maps deterministically to the n-th host address of each pool; the store
// only has to remember which slots are taken.
package ipam

import (
	"errors"
	"math/big"
	"net/netip"
)

// Reserved host offsets at the start of each pool: .0 is the network, .1 is
// the gateway and plain resolver, .2 the ad/tracker-blocking resolver and .3
// the ad/tracker/malware-blocking resolver. Devices start after them.
const reserved = 8

// ErrExhausted is returned when a slot falls outside the pool.
var ErrExhausted = errors.New("address pool exhausted")

// Pools holds the IPv4 and IPv6 tunnel prefixes.
type Pools struct {
	V4 netip.Prefix
	V6 netip.Prefix
}

// Default pools: 10.64.0.0/10 gives ~4M devices, enough for any one fleet.
var Default = Pools{
	V4: netip.MustParsePrefix("10.64.0.0/10"),
	V6: netip.MustParsePrefix("fd68:6172:626f:7200::/64"),
}

// Capacity is the number of device slots the IPv4 pool can hold.
func (p Pools) Capacity() int {
	bits := 32 - p.V4.Bits()
	return (1 << bits) - reserved - 1 // minus the broadcast address
}

// Addresses returns the tunnel addresses for a device slot.
func (p Pools) Addresses(slot int) (v4, v6 netip.Addr, err error) {
	if slot < 0 || slot >= p.Capacity() {
		return v4, v6, ErrExhausted
	}
	v4 = offset(p.V4.Masked().Addr(), int64(reserved+slot))
	v6 = offset(p.V6.Masked().Addr(), int64(reserved+slot))
	return v4, v6, nil
}

// Gateway returns the n-th reserved address (1 = gateway, 2/3 = filtered DNS).
func (p Pools) Gateway(n int) (v4, v6 netip.Addr) {
	return offset(p.V4.Masked().Addr(), int64(n)), offset(p.V6.Masked().Addr(), int64(n))
}

func offset(base netip.Addr, n int64) netip.Addr {
	b := base.AsSlice()
	v := new(big.Int).SetBytes(b)
	v.Add(v, big.NewInt(n))
	out := v.FillBytes(make([]byte, len(b)))
	a, _ := netip.AddrFromSlice(out)
	return a
}
