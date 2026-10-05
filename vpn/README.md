# Harbor VPN

An iPhone VPN built to be the one people recommend: one tap to connect, fast
WireGuard, real privacy, and none of the billing tricks users hate in the big
brands. This folder holds the whole product:

| Part | What it is |
|---|---|
| `ios/` | The SwiftUI app, its WireGuard packet tunnel and its widgets |
| `backend/` | The Go control plane (`harbor-api`) and the agent each VPN server runs (`harbor-node`) |
| `backend/deploy/` | One-command VPN server setup, systemd units, an example server list |

## What users get

**Easy**
- One big button. Smart Location picks the fastest server by measuring real
  latency to each one and avoiding busy servers.
- Locations searchable by country or city, with favorites (swipe), recent
  locations on the home screen, and live latency and load for each.
- Connect without opening the app: home and lock screen widgets, a Control
  Center toggle (iOS 18), Siri ("Connect Harbor"), Shortcuts and the Action
  Button.
- Live Activity on the Lock Screen and in the Dynamic Island, with a session
  timer and a Disconnect button.
- Auto-connect on public Wi-Fi, with a "Trust this network" list for home, or
  stay connected everywhere.
- Plain-language errors, and the VPN still connects when Harbor's API is
  unreachable (on a captive portal, say), using the installed profile.

**Private**
- No email or password. An account is a random 16-digit number, and the
  server stores only a keyed hash of it.
- WireGuard keys are generated on the iPhone, kept in the Keychain on this
  device only, and rotated automatically (daily, weekly or monthly).
- No activity, DNS or connection logs. VPN servers keep the journal in RAM
  for at most an hour, and they forget a client's real IP from kernel memory
  a few minutes after the client goes idle.
- Threat Protection: built-in ad, tracker and malware blocking through
  Harbor's own DNS inside the tunnel. No third-party DNS.
- Opt-in "Block traffic outside VPN" kill switch, with an honest explanation
  of the iOS limits (see below).
- App Store purchases are verified offline from Apple's signed transaction.
  No Apple ID or receipt is stored.

**Fair**
- Free plan: unlimited data, no ads, a set of free locations, 1 device.
- Harbor Plus: every location, 7 devices, the same price at every renewal,
  and Apple's own subscription sheet, so the terms are never hidden.

## Architecture

```
iPhone                                           Your servers
┌───────────────────────────────┐   HTTPS   ┌──────────────────────────┐
│ Harbor app (SwiftUI)          │──────────▶│ harbor-api (Go)          │
│  · accounts, server list,     │           │  accounts (hashed),      │
│    Smart Location, settings   │           │  device keys, server     │
│  · installs VPN profile       │           │  list + live load,       │
│ PacketTunnel extension        │           │  App Store verification  │
│  · WireGuardKit + wireguard-go│           └──────────▲───────────────┘
│ Widgets / Control / Live Act. │                      │ peers & heartbeats
└───────────────┬───────────────┘           ┌──────────┴───────────────┐
                │ WireGuard UDP 51820       │ VPN node (Ubuntu)        │
                └──────────────────────────▶│ wg0 + harbor-node agent  │
                                            │ unbound: .1 plain,       │
                                            │ .2 ads, .3 ads+malware   │
                                            └──────────────────────────┘
```

- Each device gets one tunnel address that is valid on every server, so
  switching location never needs a new registration.
- Nodes pull the peer list every 10 seconds and send heartbeats. A free
  account is admitted only to free servers, and an expired subscription drops
  off paid servers automatically.
- Servers that stop sending heartbeats are hidden from the app within 90
  seconds.

## Running it

### 1. Backend

```bash
cd vpn/backend
go test ./...
GOOS=linux GOARCH=amd64 go build -o bin/ ./cmd/...
```

**API host**
1. Copy `bin/harbor-api` to `/usr/local/bin`.
2. Install `deploy/harbor-api.service`.
3. Put `HARBOR_ACCOUNT_SECRET=$(openssl rand -hex 32)` in `/etc/harbor/api.env`.
4. Copy `deploy/servers.example.json` to `/etc/harbor/servers.json`.
5. Download Apple Root CA - G3 from
   https://www.apple.com/certificateauthority/ and save it as
   `/etc/harbor/AppleRootCA-G3.cer`.
6. Put Caddy in front of the API with `deploy/Caddyfile`, which handles TLS.

**Each VPN node** (Ubuntu 24.04)
1. Copy the `backend/` folder, with `bin/` built, onto the node.
2. Run `sudo HARBOR_API=https://api.yourdomain ./deploy/node-setup.sh`.
3. Paste the public key and token hash it prints into `servers.json`.
4. Restart `harbor-api`.

### 2. iPhone app (on a Mac with Xcode 16+ and Go 1.23–1.25)

```bash
brew install xcodegen go
cd vpn/ios
./scripts/build-wireguard-go.sh   # builds the WireGuard core (once)
xcodegen generate
open Harbor.xcodeproj
```

Then, in Xcode:
1. Set your team in the project settings.
2. Set `HARBOR_API_URL` in `project.yml` to your API's address, then run
   `xcodegen generate` again.
3. Change the bundle ID prefix `net.harborvpn` to your own if you need to.
4. Run on a real iPhone. The VPN itself doesn't run in the Simulator, though
   the app UI does.

**App Store Connect**
- Create the auto-renewable subscriptions `harbor.monthly` and
  `harbor.yearly` in one group.
- Enable the App Group `group.net.harborvpn` and the Network Extension
  capability for all three bundle IDs.
- Enable Access Wi-Fi Information for the app.

## Before you ship: things only you can do

- **Apple developer account as an organization.** Guideline 5.4 requires VPN
  apps to come from an organization account. Moving the app over from a
  personal account later has led to rejections.
- **Servers.** You need at least one VPN node and one API host. A €5/month
  VPS per location is enough to start.
- **Privacy policy and terms** at the URLs in
  `ios/Shared/Common/Constants.swift`, plus accurate App Store privacy labels.
  With this code the honest label is "Data Not Collected".
- **Licensing.** Some countries require a license to offer a VPN. List them in
  your App Review notes.
- **An audit** of the no-logs setup, once you have users. It's the biggest
  trust signal in this market.

## Honest limits (iOS)

- **Per-app split tunneling** isn't possible for App Store VPN apps. Apple
  only allows it through MDM.
- **Kill switch.** Even with "Block traffic outside VPN" on, some Apple
  services can bypass any VPN. That mode also blocks App Store downloads,
  including Harbor's own updates, while it is reconnecting, which is why it's
  opt-in.
- **Extension memory.** iOS limits the packet tunnel to about 50 MB, which is
  why everything except WireGuard itself runs in the app.

## Roadmap ideas

- Multi-hop, quantum-resistant handshakes and traffic-pattern padding (like
  Mullvad's DAITA).
- An obfuscated mode for censored networks (AmneziaWG or WireGuard over TLS).
- Replacing the Go WireGuard bridge with a Rust one (GotaTun / BoringTun) to
  cut memory use.
- macOS and iPad layouts. The core package already builds for macOS.
