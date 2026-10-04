//go:build linux

package node

import (
	"net"

	"golang.zx2c4.com/wireguard/wgctrl"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

// Kernel drives a kernel WireGuard interface over netlink.
type Kernel struct {
	Name   string
	client *wgctrl.Client
}

// OpenKernel connects to the named interface (e.g. "wg0").
func OpenKernel(name string) (*Kernel, error) {
	c, err := wgctrl.New()
	if err != nil {
		return nil, err
	}
	if _, err := c.Device(name); err != nil {
		c.Close()
		return nil, err
	}
	return &Kernel{Name: name, client: c}, nil
}

func (k *Kernel) Peers() (map[string]PeerState, error) {
	d, err := k.client.Device(k.Name)
	if err != nil {
		return nil, err
	}
	out := make(map[string]PeerState, len(d.Peers))
	for _, p := range d.Peers {
		var ips []string
		for _, n := range p.AllowedIPs {
			ips = append(ips, n.String())
		}
		out[p.PublicKey.String()] = PeerState{
			AllowedIPs: ips, HasEndpoint: p.Endpoint != nil,
			LastHandshake: p.LastHandshakeTime, RxBytes: p.ReceiveBytes, TxBytes: p.TransmitBytes,
		}
	}
	return out, nil
}

func (k *Kernel) Configure(upsert []Peer, remove []string) error {
	var removals, cfgs []wgtypes.PeerConfig
	for _, r := range remove {
		key, err := wgtypes.ParseKey(r)
		if err != nil {
			return err
		}
		removals = append(removals, wgtypes.PeerConfig{PublicKey: key, Remove: true})
	}
	for _, p := range upsert {
		key, err := wgtypes.ParseKey(p.PublicKey)
		if err != nil {
			return err
		}
		var nets []net.IPNet
		for _, s := range p.AllowedIPs {
			_, n, err := net.ParseCIDR(s)
			if err != nil {
				return err
			}
			nets = append(nets, *n)
		}
		cfgs = append(cfgs, wgtypes.PeerConfig{PublicKey: key, ReplaceAllowedIPs: true, AllowedIPs: nets})
	}
	// Removals go first in their own call so a scrubbed peer is really
	// dropped (losing its endpoint) before it is added back.
	if len(removals) > 0 {
		if err := k.client.ConfigureDevice(k.Name, wgtypes.Config{Peers: removals}); err != nil {
			return err
		}
	}
	if len(cfgs) > 0 {
		return k.client.ConfigureDevice(k.Name, wgtypes.Config{Peers: cfgs})
	}
	return nil
}
