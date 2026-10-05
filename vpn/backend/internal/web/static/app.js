// Harbor web app. Plain JavaScript, no build step, no third-party requests.
//
// iOS doesn't let a web page open a VPN tunnel, so this app does the rest:
// account, location choice, Threat Protection, and a WireGuard
// configuration for Apple's free WireGuard app, which runs the tunnel.
// The private key is generated here and stays on this device; Harbor's
// server only ever receives the public key.
"use strict";

(() => {
  const app = document.getElementById("app");
  const toastEl = document.getElementById("toast");
  const WIREGUARD_APP = "https://apps.apple.com/app/wireguard/id1441195209";
  const DNS_KEY = { off: "standard", adsTrackers: "block_ads", adsTrackersMalware: "block_ads_malware" };

  // ---- storage (Private Browsing can make localStorage throw) -------------
  const store = {
    get(key, fallback = null) {
      try {
        const v = localStorage.getItem("harbor." + key);
        return v == null ? fallback : JSON.parse(v);
      } catch { return fallback; }
    },
    set(key, value) {
      try {
        if (value == null) localStorage.removeItem("harbor." + key);
        else localStorage.setItem("harbor." + key, JSON.stringify(value));
      } catch { /* ignore */ }
    },
  };

  const state = {
    view: "loading",
    number: store.get("account"),
    account: null,
    servers: [],
    dns: null,
    locationId: store.get("location"),
    protection: store.get("protection", "adsTrackers"),
    newNumber: null,
    busy: false,
    query: "",
    setup: null, // { name, text, location }
    revealNumber: false,
  };

  // ---- tiny DOM helper ------------------------------------------------------
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v; // CSSOM, so the strict CSP still applies
      else if (k === "trustedHTML") el.innerHTML = v; // only our own constants / generated SVG
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : String(kid));
    }
    return el;
  }

  const ICONS = {
    user: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/></svg>',
    chev: '<svg width="9" height="15" viewBox="0 0 9 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1.5 1.5l6 6-6 6"/></svg>',
  };
  const icon = (name, cls) => h("span", { class: cls, "aria-hidden": "true", trustedHTML: ICONS[name] });

  let toastTimer;
  function toast(message) {
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
  }

  // ---- API ------------------------------------------------------------------
  async function api(method, path, body) {
    const headers = { Accept: "application/json" };
    if (state.number) headers.Authorization = "Account " + state.number;
    if (body) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    } catch {
      throw Object.assign(new Error("Can't reach Harbor. Check your internet connection."), { code: "offline" });
    }
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      throw Object.assign(new Error((data && data.message) || `Something went wrong (${res.status}).`),
        { code: data && data.code, status: res.status });
    }
    return data;
  }

  async function loadServers() {
    try {
      const list = await api("GET", "/v1/servers");
      state.servers = list.servers;
      state.dns = list.dns;
      store.set("servers", list);
    } catch (e) {
      const cached = store.get("servers");
      if (cached) { state.servers = cached.servers; state.dns = cached.dns; }
      else throw e;
    }
  }

  async function refreshAccount() {
    state.account = await api("GET", "/v1/account");
  }

  // ---- keys -----------------------------------------------------------------
  const b64 = (bytes) => btoa(String.fromCharCode(...bytes));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

  function storedKey() {
    const priv = store.get("key");
    if (!priv) return null;
    try {
      const raw = unb64(priv);
      if (raw.length !== 32) return null;
      return { priv, pub: b64(nacl.scalarMult.base(raw)) };
    } catch { return null; }
  }

  function newKey() {
    const kp = nacl.box.keyPair(); // X25519, from the browser's secure RNG
    return { priv: b64(kp.secretKey), pub: b64(kp.publicKey) };
  }

  function deviceName() {
    const ua = navigator.userAgent;
    if (/iPhone/.test(ua)) return "iPhone";
    if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "iPad";
    if (/Android/.test(ua)) return "Android";
    if (/Macintosh/.test(ua)) return "Mac";
    return "Web";
  }

  function thisDevice() {
    const key = storedKey();
    return key && state.account ? state.account.devices.find((d) => d.public_key === key.pub) : null;
  }

  /** Makes sure this browser has a key registered on the account. */
  async function ensureDevice() {
    let key = storedKey();
    const existing = thisDevice();
    if (key && existing) return { key, device: existing };
    if (!key) key = newKey();
    store.set("key", key.priv); // save before registering so a crash can't orphan it
    let device;
    try {
      device = await api("POST", "/v1/devices", { public_key: key.pub, name: deviceName() });
    } catch (e) {
      if (e.code !== "key_in_use") throw e;
      key = newKey(); // the stored key belonged to another account
      store.set("key", key.priv);
      device = await api("POST", "/v1/devices", { public_key: key.pub, name: deviceName() });
    }
    await refreshAccount();
    return { key, device };
  }

  // ---- locations ------------------------------------------------------------
  const tier = () => (state.account && state.account.tier) || "free";
  const locId = (s) => `${s.country_code}-${s.city.toLowerCase().replace(/\s+/g, "-")}`;
  const flag = (cc) => {
    const up = (cc || "").toUpperCase();
    if (!/^[A-Z]{2}$/.test(up)) return "🌐";
    return String.fromCodePoint(...[...up].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
  };

  function locations() {
    const byId = new Map();
    for (const s of state.servers) {
      const id = locId(s);
      if (!byId.has(id)) byId.set(id, { id, cc: s.country_code, country: s.country, city: s.city, servers: [] });
      byId.get(id).servers.push(s);
    }
    const out = [...byId.values()];
    for (const l of out) {
      l.free = l.servers.some((s) => s.free);
      l.load = Math.min(...l.servers.map((s) => s.load));
      l.name = `${l.city}, ${l.country}`;
    }
    return out.sort((a, b) => a.country.localeCompare(b.country) || a.city.localeCompare(b.city));
  }

  const canUse = (loc) => tier() === "paid" || loc.free;
  const eligible = (servers) => (tier() === "paid" ? servers : servers.filter((s) => s.free));

  function minBy(list, keyFn) {
    let best = null, bestKey = null;
    for (const item of list) {
      const k = keyFn(item);
      if (best === null || compareTuple(k, bestKey) < 0) { best = item; bestKey = k; }
    }
    return best;
  }
  function compareTuple(a, b) {
    for (let i = 0; i < a.length; i++) {
      if (a[i] < b[i]) return -1;
      if (a[i] > b[i]) return 1;
    }
    return 0;
  }

  function regionCode() {
    try { return (new Intl.Locale(navigator.language).maximize().region || "").toLowerCase(); }
    catch { return ""; }
  }

  /** Same rules as the iPhone app's Smart Location, minus latency probing. */
  function smartServer() {
    const candidates = eligible(state.servers);
    if (!candidates.length) return null;
    const local = candidates.filter((s) => s.country_code === regionCode());
    if (local.length) return minBy(local, (s) => [s.load, s.id]);
    // The time zone gives a rough longitude (15° per hour).
    const lon = (-new Date().getTimezoneOffset() / 60) * 15;
    const dist = (a) => { const d = Math.abs(a - lon) % 360; return Math.min(d, 360 - d); };
    return minBy(candidates, (s) => [Math.round(dist(s.longitude)), s.load, s.id]);
  }

  function selectedLocation() {
    return state.locationId ? locations().find((l) => l.id === state.locationId) || null : null;
  }

  function pickServer() {
    const loc = selectedLocation();
    if (loc) {
      if (!canUse(loc)) throw new Error(`${loc.city} is part of Harbor Plus.`);
      return minBy(eligible(loc.servers), (s) => [s.load, s.id]);
    }
    return smartServer();
  }

  // ---- WireGuard config -----------------------------------------------------
  function wireGuardConfig(key, device, server) {
    const dns = (state.dns && state.dns[DNS_KEY[state.protection]]) || [];
    return [
      "[Interface]",
      `PrivateKey = ${key.priv}`,
      `Address = ${device.ipv4_address}, ${device.ipv6_address}`,
      `DNS = ${dns.join(", ")}`,
      "MTU = 1280",
      "",
      "[Peer]",
      `PublicKey = ${server.public_key}`,
      "AllowedIPs = 0.0.0.0/0, ::/0",
      `Endpoint = ${server.ipv4}:${server.port}`,
      "PersistentKeepalive = 25",
      "",
    ].join("\n");
  }

  function tunnelName(server) {
    const city = server.city.normalize("NFD").replace(/[^A-Za-z0-9]+/g, "");
    return `Harbor-${city}`.slice(0, 32);
  }

  async function saveFile(name, text) {
    const file = new File([text], name, { type: "text/plain" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file] });
        return true;
      } catch (e) {
        if (e.name === "AbortError") return false;
      }
    }
    const url = URL.createObjectURL(file);
    const a = h("a", { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    return true;
  }

  function qrSVG(text) {
    const qr = qrcode(0, "L");
    qr.addData(text);
    qr.make();
    return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  }

  // ---- actions --------------------------------------------------------------
  async function run(fn) {
    if (state.busy) return;
    state.busy = true;
    render();
    try { await fn(); }
    catch (e) {
      if (e.code === "device_limit") {
        state.view = "account";
        toast("Remove a device you no longer use, then try again.");
      } else {
        toast(e.message || "Something went wrong.");
      }
    } finally {
      state.busy = false;
      render();
    }
  }

  const createAccount = () => run(async () => {
    state.number = null;
    const acct = await api("POST", "/v1/accounts");
    state.number = acct.number;
    store.set("account", acct.number);
    state.account = acct;
    state.newNumber = acct.number;
    state.view = "reveal";
  });

  const signIn = (input) => run(async () => {
    const digits = input.replace(/[\s-]/g, "");
    if (!/^\d{16}$/.test(digits)) throw new Error("Account numbers have 16 digits.");
    state.number = digits;
    try { await refreshAccount(); }
    catch (e) { state.number = null; throw e; }
    store.set("account", digits);
    state.view = "home";
  });

  const signOut = () => {
    if (!confirm("Sign out of Harbor on this device?\n\nTunnels you added to WireGuard keep working. To revoke them, remove this device under Devices first.")) return;
    store.set("account", null);
    store.set("key", null);
    state.number = null;
    state.account = null;
    state.view = "welcome";
    render();
  };

  const removeDevice = (device) => {
    const mine = thisDevice() && thisDevice().id === device.id;
    const msg = mine
      ? "Remove this device? Harbor tunnels already added to WireGuard on it will stop working."
      : `Remove “${device.name}”? Its Harbor tunnels will stop working.`;
    if (!confirm(msg)) return;
    run(async () => {
      await api("DELETE", `/v1/devices/${encodeURIComponent(device.id)}`);
      if (mine) store.set("key", null);
      await refreshAccount();
      toast("Device removed.");
    });
  };

  const prepareSetup = () => run(async () => {
    if (!state.servers.length) await loadServers();
    const server = pickServer();
    if (!server) throw new Error("No servers are available right now. Please try again shortly.");
    const { key, device } = await ensureDevice();
    const text = wireGuardConfig(key, device, server);
    state.setup = { name: tunnelName(server) + ".conf", text, server };
    state.view = "setup";
  });

  function choose(loc) {
    if (loc && !canUse(loc)) { toast(`${loc.city} is part of Harbor Plus.`); return; }
    state.locationId = loc ? loc.id : null;
    store.set("location", state.locationId);
    go("home");
  }

  function go(view) {
    state.view = view;
    render();
    window.scrollTo(0, 0);
  }

  // ---- views ----------------------------------------------------------------
  function render() {
    const view = VIEWS[state.view] || VIEWS.loading;
    app.replaceChildren(view());
  }

  const VIEWS = {
    loading: () => h("div", { class: "center" }, h("div", { class: "spinner", "aria-label": "Loading" })),

    welcome: () => h("div", {},
      h("div", { class: "hero" },
        h("img", { class: "logo", src: "/icon-180.png", alt: "" }),
        h("h1", { style: "margin-top:22px" }, "Private by default."),
        h("p", { class: "muted" }, "Protect everything you do on this iPhone. No email, no tracking, no logs.")),
      h("ul", { class: "features" },
        feature("⚡️", "WireGuard speed, easy on battery"),
        feature("🛡️", "Blocks ads, trackers and malware"),
        feature("🔑", "Your key is made on this device"),
        feature("♾️", "Free plan with unlimited data")),
      h("div", { class: "stack" },
        h("button", { class: "btn", onclick: createAccount, disabled: state.busy },
          state.busy ? h("span", { class: "spinner", style: "width:20px;height:20px;border-color:rgba(255,255,255,.35);border-top-color:#fff" }) : "Get started — no sign-up"),
        h("button", { class: "btn plain", onclick: () => go("signin") }, "I have an account number")),
      h("p", { class: "muted tiny", style: "text-align:center;margin-top:18px" },
        "Harbor runs inside Apple's free WireGuard app. We'll walk you through it.")),

    reveal: () => h("div", {},
      h("div", { class: "hero" }, h("div", { style: "font-size:44px" }, "🔑")),
      h("h2", { style: "text-align:center" }, "Your account number"),
      h("p", { class: "muted small", style: "text-align:center" },
        "This is your login: there's no email or password. Save it in your password manager to sign in on other devices."),
      h("div", { class: "number mono" }, groupNumber(state.newNumber)),
      h("div", { class: "stack", style: "margin-top:16px" },
        h("button", { class: "btn secondary", onclick: () => copy(state.newNumber, "Account number copied.") }, "Copy"),
        h("button", { class: "btn", onclick: () => { state.newNumber = null; go("home"); } }, "I've saved it"))),

    signin: () => {
      const input = h("input", {
        class: "number mono", inputmode: "numeric", autocomplete: "off", placeholder: "0000 0000 0000 0000",
        "aria-label": "Account number", maxlength: "19",
        oninput: (e) => { e.target.value = groupNumber(e.target.value.replace(/\D/g, "").slice(0, 16)); },
        onkeydown: (e) => { if (e.key === "Enter") signIn(input.value); },
      });
      setTimeout(() => input.focus(), 50);
      return h("div", {},
        h("button", { class: "back", onclick: () => go(state.number ? "home" : "welcome") }, "‹ Back"),
        h("h2", {}, "Sign in"),
        h("p", { class: "muted small" }, "Enter the 16-digit number you saved when you created your account."),
        input,
        h("div", { style: "margin-top:16px" },
          h("button", { class: "btn", onclick: () => signIn(input.value), disabled: state.busy }, "Sign in")));
    },

    home: () => {
      const loc = selectedLocation();
      const smart = !loc;
      const added = store.get("added", []);
      const free = tier() === "free";
      const freeCount = locations().filter((l) => l.free).length;
      return h("div", {},
        h("div", { class: "bar" },
          h("div", { class: "title" }, h("img", { src: "/icon-180.png", alt: "" }), "Harbor"),
          h("button", { class: "icon-btn", onclick: () => go("account"), "aria-label": "Account" }, icon("user"))),

        h("div", { class: "card status" + (added.length ? " ready" : "") },
          h("div", { class: "dot", "aria-hidden": "true" }, added.length ? "✅" : "🛡️"),
          h("div", {},
            h("strong", {}, added.length ? "Harbor is set up" : "Let's protect this iPhone"),
            h("div", { class: "muted small" }, added.length
              ? "Turn it on or off in the WireGuard app. You'll see “VPN” in the status bar when you're protected."
              : "Pick a location, then add it to the free WireGuard app. It takes about a minute."))),

        h("h3", {}, "Location"),
        h("div", { class: "list" },
          h("button", { class: "row", onclick: () => go("locations") },
            h("span", { class: "flag" }, smart ? "⚡️" : flag(loc.cc)),
            h("span", { class: "grow" },
              h("div", {}, smart ? "Smart Location" : loc.name),
              h("div", { class: "sub" }, smart ? "The best server for where you are" : loadText(loc.load))),
            icon("chev", "chev"))),

        h("h3", {}, "Threat Protection"),
        h("div", { class: "seg", role: "group", "aria-label": "Threat Protection" },
          segButton("off", "Off"), segButton("adsTrackers", "Ads & trackers"), segButton("adsTrackersMalware", "+ Malware")),
        h("p", { class: "muted tiny", style: "margin:8px 4px 0" },
          "Blocks ads, trackers and dangerous sites in every app, using Harbor's own DNS. Changing this needs the location to be added again."),

        h("div", { style: "margin-top:26px" },
          h("button", { class: "btn", onclick: prepareSetup, disabled: state.busy },
            state.busy ? "Preparing…" : added.length ? "Add this location to WireGuard" : "Set up Harbor")),

        free ? h("div", { class: "banner" },
          h("span", { class: "ico" }, "✨"),
          h("div", { class: "small" }, h("strong", {}, "Harbor Free"),
            h("div", { class: "muted" }, `Unlimited data and no ads. ${freeCount} free location${freeCount === 1 ? "" : "s"}, 1 device.`))) : null,

        isIOSSafariTab() ? h("div", { class: "banner" },
          h("span", { class: "ico" }, "📲"),
          h("div", { class: "small" }, h("strong", {}, "Keep Harbor on your Home Screen"),
            h("div", { class: "muted" }, "Tap the Share button, then “Add to Home Screen”."))) : null);
    },

    locations: () => {
      const listEl = h("div", {});
      const search = h("input", {
        class: "search", type: "search", placeholder: "Search country or city", value: state.query,
        "aria-label": "Search locations",
        oninput: (e) => { state.query = e.target.value; fillLocations(listEl); },
      });
      fillLocations(listEl);
      return h("div", {},
        h("div", { class: "bar" },
          h("button", { class: "back", onclick: () => go("home") }, "‹ Back"),
          h("strong", {}, "Locations"),
          h("span", { style: "width:52px" })),
        search,
        listEl);
    },

    setup: () => {
      const s = state.setup;
      const added = store.get("added", []);
      const already = added.includes(s.name);
      return h("div", {},
        h("button", { class: "back", onclick: () => go("home") }, "‹ Done"),
        h("h2", {}, "Add Harbor to WireGuard"),
        h("p", { class: "muted small" }, `${flag(s.server.country_code)} ${s.server.city}, ${s.server.country}`),
        h("ol", { class: "steps card" },
          h("li", {},
            h("strong", {}, "Get WireGuard"),
            h("div", { class: "muted small" }, "It's free on the App Store and runs the secure tunnel. Skip this if you have it."),
            h("a", { class: "btn secondary", style: "margin-top:10px;min-height:44px", href: WIREGUARD_APP, target: "_blank", rel: "noopener" }, "Open App Store")),
          h("li", {},
            h("strong", {}, "Save your Harbor file"),
            h("div", { class: "muted small" }, "Tap below, then choose “Save to Files”."),
            h("button", {
              class: "btn", style: "margin-top:10px;min-height:48px",
              onclick: async () => {
                if (await saveFile(s.name, s.text)) {
                  if (!already) store.set("added", [...added, s.name]);
                  toast("Saved. Now open WireGuard.");
                }
              },
            }, `Save ${s.name}`)),
          h("li", {},
            h("strong", {}, "Import it in WireGuard"),
            h("div", { class: "muted small" }, "Open WireGuard, tap +, choose “Create from file or archive”, and pick the file you just saved. Allow the VPN configuration when iOS asks.")),
          h("li", {},
            h("strong", {}, "Turn it on"),
            h("div", { class: "muted small" }, "Flip the switch next to “" + s.name.replace(/\.conf$/, "") + "”. To connect automatically, tap the tunnel, then Edit → On-Demand, and turn on Wi-Fi and Cellular."))),

        h("details", {},
          h("summary", {}, "Setting up another device? Show QR code"),
          h("p", { class: "muted small" }, "In WireGuard on the other device, tap + → “Create from QR code”. Both devices then share this key, so use them one at a time."),
          h("div", { class: "qr", trustedHTML: qrSVG(s.text) })),
        h("details", {},
          h("summary", {}, "Show configuration"),
          h("pre", { class: "config mono" }, s.text)),
        h("p", { class: "muted tiny", style: "margin-top:16px" },
          "This file contains your private key. Keep it to yourself. Want another location? Pick it on the home screen and add it the same way: each one appears as its own switch in WireGuard."));
    },

    account: () => {
      const acct = state.account;
      const mine = thisDevice();
      return h("div", {},
        h("button", { class: "back", onclick: () => go("home") }, "‹ Back"),
        h("h2", {}, "Account"),
        h("h3", {}, "Account number"),
        h("div", { class: "card" },
          h("button", {
            class: "number mono", style: "width:100%;border:0;color:inherit;cursor:pointer",
            onclick: () => { state.revealNumber = !state.revealNumber; render(); },
            "aria-label": "Show or hide account number",
          }, state.revealNumber ? groupNumber(state.number) : "•••• •••• •••• " + String(state.number).slice(-4)),
          h("button", { class: "btn plain", onclick: () => copy(state.number, "Account number copied.") }, "Copy account number"),
          h("p", { class: "muted tiny", style: "margin:4px 0 0" }, "Keep it safe. We can't recover it for you.")),

        h("h3", {}, "Plan"),
        h("div", { class: "list" },
          h("div", { class: "row" },
            h("span", { class: "grow" }, acct && acct.tier === "paid" ? "Harbor Plus" : "Harbor Free"),
            acct && acct.paid_until && acct.tier === "paid"
              ? h("span", { class: "pill good" }, "until " + new Date(acct.paid_until).toLocaleDateString())
              : h("span", { class: "pill" }, "Unlimited data"))),

        h("h3", {}, `Devices (${acct ? acct.devices.length : 0} of ${acct ? acct.max_devices : 1})`),
        h("div", { class: "list" },
          (acct && acct.devices.length ? acct.devices : []).map((d) =>
            h("div", { class: "row" },
              h("span", { class: "grow" },
                h("div", {}, d.name, mine && mine.id === d.id ? h("span", { class: "pill", style: "margin-left:8px" }, "This device") : null),
                h("div", { class: "sub" }, "Added " + new Date(d.created).toLocaleDateString())),
              h("button", { class: "btn danger", style: "width:auto;min-height:36px;font-size:15px", onclick: () => removeDevice(d) }, "Remove"))),
          acct && acct.devices.length ? null : h("div", { class: "row muted small" }, "No devices yet.")),

        h("h3", {}, "How Harbor protects you"),
        h("div", { class: "card small" },
          h("p", {}, "🔑 Your WireGuard private key is created in this browser and never sent to us."),
          h("p", {}, "🧾 No activity, DNS or connection logs. Servers keep their system log in memory only, for at most an hour."),
          h("p", { style: "margin:0" }, "🧹 A few minutes after you go idle, the server forgets the IP address you connected from.")),

        h("div", { style: "margin-top:24px" },
          h("button", { class: "btn danger", onclick: signOut }, "Sign out")));
    },
  };

  function fillLocations(listEl) {
    const q = state.query.trim().toLowerCase();
    const all = locations()
      .filter((l) => !q || l.country.toLowerCase().includes(q) || l.city.toLowerCase().includes(q) || l.cc === q)
      .sort((a, b) => Number(canUse(b)) - Number(canUse(a))); // usable first; sort is stable
    const rows = [];
    if (!q) {
      rows.push(h("button", { class: "row" + (state.locationId ? "" : " selected"), onclick: () => choose(null) },
        h("span", { class: "flag" }, "⚡️"),
        h("span", { class: "grow" }, h("div", {}, "Smart Location"), h("div", { class: "sub" }, "The best server for where you are")),
        state.locationId ? null : h("span", { class: "check" }, "✓")));
    }
    for (const l of all) {
      const usable = canUse(l);
      rows.push(h("button", {
        class: "row" + (state.locationId === l.id ? " selected" : ""),
        "aria-disabled": usable ? null : "true",
        onclick: () => choose(l),
      },
        h("span", { class: "flag" }, flag(l.cc)),
        h("span", { class: "grow" }, h("div", {}, l.city), h("div", { class: "sub" }, l.country)),
        usable ? loadBar(l.load) : h("span", { "aria-label": "Harbor Plus" }, "🔒"),
        state.locationId === l.id ? h("span", { class: "check" }, "✓") : null));
    }
    listEl.replaceChildren(
      rows.length ? h("div", { class: "list", style: "margin-top:8px" }, rows)
        : h("p", { class: "muted", style: "text-align:center;margin-top:40px" },
          state.servers.length ? `No locations match “${state.query}”.` : "No locations are online right now."));
  }

  function feature(emoji, text) {
    return h("li", {}, h("span", { class: "ico", "aria-hidden": "true" }, emoji), h("span", {}, text));
  }

  function segButton(value, label) {
    return h("button", {
      "aria-pressed": String(state.protection === value),
      onclick: () => { state.protection = value; store.set("protection", value); render(); },
    }, label);
  }

  function loadBar(load) {
    const cls = load < 60 ? "load" : load < 85 ? "load mid" : "load high";
    return h("span", { class: cls, title: `${load}% load`, "aria-label": `${load}% load` },
      h("span", { style: `width:${Math.max(8, load)}%` }));
  }

  const loadText = (load) => (load < 60 ? "Not busy" : load < 85 ? "Busy" : "Very busy") + ` · ${load}% load`;

  function groupNumber(n) {
    return String(n || "").replace(/(\d{4})(?=\d)/g, "$1 ");
  }

  async function copy(text, message) {
    try {
      await navigator.clipboard.writeText(text);
      toast(message);
    } catch {
      toast("Couldn't copy. Press and hold the number to copy it.");
    }
  }

  function isIOSSafariTab() {
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
    return ios && !window.navigator.standalone && !matchMedia("(display-mode: standalone)").matches;
  }

  // ---- start ----------------------------------------------------------------
  async function boot() {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
    try { await loadServers(); } catch (e) { toast(e.message); }
    if (state.number) {
      try {
        await refreshAccount();
        state.view = "home";
      } catch (e) {
        if (e.status === 401) {
          store.set("account", null);
          state.number = null;
          state.view = "welcome";
        } else {
          state.view = "home";
        }
        toast(e.message);
      }
    } else {
      state.view = "welcome";
    }
    render();
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.number && !state.busy) {
      Promise.all([loadServers(), refreshAccount()]).then(render, () => {});
    }
  });

  boot();
})();
