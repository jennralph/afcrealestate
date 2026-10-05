package account

import "testing"

func TestNewIsSixteenDigits(t *testing.T) {
	seen := map[string]bool{}
	for range 200 {
		n, err := New()
		if err != nil {
			t.Fatal(err)
		}
		if _, err := Normalize(n); err != nil {
			t.Fatalf("%q: %v", n, err)
		}
		if seen[n] {
			t.Fatalf("duplicate %q", n)
		}
		seen[n] = true
	}
}

func TestNormalize(t *testing.T) {
	good := map[string]string{
		"1234567890123456":    "1234567890123456",
		"1234 5678 9012 3456": "1234567890123456",
		"1234-5678-9012-3456": "1234567890123456",
	}
	for in, want := range good {
		if got, err := Normalize(in); err != nil || got != want {
			t.Errorf("%q: %q %v", in, got, err)
		}
	}
	for _, in := range []string{"", "123", "12345678901234567", "1234x5678901234567", "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦"} {
		if _, err := Normalize(in); err == nil {
			t.Errorf("%q accepted", in)
		}
	}
}

func TestHashIsKeyed(t *testing.T) {
	a, b := NewHasher([]byte("one")), NewHasher([]byte("two"))
	if a.Hash("1234567890123456") == b.Hash("1234567890123456") {
		t.Fatal("hash must depend on the secret")
	}
}
