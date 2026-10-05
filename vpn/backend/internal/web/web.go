// Package web serves Harbor's web app: the version people can use from
// iPhone Safari (or "Add to Home Screen") before the native app ships.
//
// iOS does not let a web page create a VPN, so the web app does everything
// else — account, locations, Threat Protection — and hands the user a
// WireGuard configuration for Apple's App Store WireGuard app, which runs
// the tunnel. Keys are generated in the browser; the server only ever sees
// the public key.
package web

import (
	"embed"
	"io/fs"
	"net/http"
	"path"
	"strings"
)

//go:embed static
var files embed.FS

// csp allows nothing but this origin. The page holds a WireGuard private
// key, so no third-party script, style or connection is ever permitted.
const csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
	"connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; " +
	"form-action 'none'; frame-ancestors 'none'; object-src 'none'"

// Handler serves the embedded web app.
func Handler() http.Handler {
	static, err := fs.Sub(files, "static")
	if err != nil {
		panic(err)
	}
	fileServer := http.FileServerFS(static)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("Content-Security-Policy", csp)
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()")
		h.Set("Cross-Origin-Opener-Policy", "same-origin")

		p := r.URL.Path
		if p == "/" || p == "/index.html" {
			r2 := *r
			u := *r.URL
			u.Path = "/"
			r2.URL = &u
			h.Set("Cache-Control", "no-cache")
			fileServer.ServeHTTP(w, &r2)
			return
		}
		switch path.Ext(p) {
		case ".webmanifest":
			h.Set("Content-Type", "application/manifest+json")
		}
		if p == "/sw.js" || strings.HasSuffix(p, ".webmanifest") {
			h.Set("Cache-Control", "no-cache")
		} else {
			// The service worker revalidates; keep browser caching short so
			// an update reaches everyone within minutes.
			h.Set("Cache-Control", "public, max-age=300")
		}
		if _, err := fs.Stat(static, strings.TrimPrefix(p, "/")); err != nil {
			http.NotFound(w, r)
			return
		}
		fileServer.ServeHTTP(w, r)
	})
}
