package web

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func get(t *testing.T, path string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec
}

func TestServesAppWithStrictHeaders(t *testing.T) {
	for _, p := range []string{"/", "/index.html"} {
		rec := get(t, p)
		if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `<script src="/app.js"`) {
			t.Fatalf("%s: %d", p, rec.Code)
		}
		csp := rec.Header().Get("Content-Security-Policy")
		if !strings.Contains(csp, "script-src 'self'") || strings.Contains(csp, "unsafe") {
			t.Fatalf("weak CSP: %q", csp)
		}
		if rec.Header().Get("Cache-Control") != "no-cache" {
			t.Fatalf("index must revalidate")
		}
	}
}

func TestAssets(t *testing.T) {
	cases := map[string]string{
		"/app.js":                  "javascript",
		"/app.css":                 "text/css",
		"/manifest.webmanifest":    "application/manifest+json",
		"/vendor/nacl-fast.min.js": "javascript",
		"/icon-180.png":            "image/png",
	}
	for p, ct := range cases {
		rec := get(t, p)
		if rec.Code != http.StatusOK || !strings.Contains(rec.Header().Get("Content-Type"), ct) {
			t.Errorf("%s: %d %q", p, rec.Code, rec.Header().Get("Content-Type"))
		}
	}
	if rec := get(t, "/sw.js"); rec.Header().Get("Cache-Control") != "no-cache" {
		t.Error("service worker must revalidate so updates ship")
	}
	if rec := get(t, "/missing.js"); rec.Code != http.StatusNotFound {
		t.Errorf("missing file: %d", rec.Code)
	}
	if rec := get(t, "/vendor/"); rec.Code != http.StatusNotFound && rec.Code != http.StatusOK {
		t.Errorf("dir: %d", rec.Code)
	}
}
