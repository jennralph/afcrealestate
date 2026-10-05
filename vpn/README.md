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

## Try it now: the web version (no Mac, no App Store)

iOS doesn't let a website create a VPN, so the web version splits the job:
Harbor's site handles your account, location and ad blocking, and Apple's
free **WireGuard** app runs the tunnel. You use the same servers, keys and
privacy as the native app. The site is served by `harbor-api` itself, at the
root URL.

**Set it up on one server (about 10 minutes)**
1. Rent a small Ubuntu 24.04 server (€4–6/month from Hetzner,
   DigitalOcean, Vultr and others). Pick the country you want to appear in.
2. In the provider's firewall, open TCP 80, TCP 443 and UDP 51820.
3. On the server, run:
   ```bash
   git clone https://github.com/jennralph/afcrealestate.git
   cd afcrealestate/vpn/backend
   sudo ./deploy/all-in-one.sh
   ```
   It installs WireGuard, ad-blocking DNS, the API, the web app and HTTPS.
   When it finishes, it prints your address, such as
   `https://203-0-113-10.sslip.io`. No domain is needed.

**Use it on your iPhone**
1. Open that address in Safari. Tap Share, then **Add to Home Screen**.
2. Tap **Get started** and save the account number it shows.
3. Tap **Set up Harbor**, then follow the 4 steps. Install WireGuard, save
   the file, import it in WireGuard, and turn it on.
4. In WireGuard, open the tunnel and tap Edit, then On-Demand. Turn on Wi-Fi
   and Cellular so it reconnects by itself.
5. Optional, for more than 1 device on your own account: run
   `sudo harbor-grant <account number> 3650` on the server.

What the web version can't do (only the native app can):
- Smart Location doesn't measure latency; it uses your region instead.
- Widgets, the Control Center toggle and the Live Activity aren't available.
- Changing location or Threat Protection means adding the tunnel to
  WireGuard again. Each location becomes its own switch there.
- The private key is kept in Safari's storage so you can add more locations.
  If you clear Safari's data, sign in again and remove the old device under
  Account.

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
