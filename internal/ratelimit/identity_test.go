package ratelimit

import (
	"strings"
	"testing"
	"time"
)

func TestPeerIdentity(t *testing.T) {
	a, err := PeerID("192.0.2.1:80")
	if err != nil {
		t.Fatal(err)
	}
	for _, address := range []string{"192.0.2.1:9000", "[::ffff:192.0.2.1]:1000"} {
		got, err := PeerID(address)
		if err != nil || got != a {
			t.Fatalf("canonical identity mismatch: %q %v", address, err)
		}
	}
	if len(a) != 64 || strings.Contains(a, "192.0.2.1") {
		t.Fatal("unbounded/raw identity")
	}
	b, _ := PeerID("[2001:db8::1%test]:80")
	c, _ := PeerID("[2001:db8::1]:90")
	if b != c {
		t.Fatal("zone changed identity")
	}
	for _, address := range []string{"", "192.0.2.1", "host.example:80", "not-an-IP:1000", "192.0.2.1:99999"} {
		if _, err := PeerID(address); err == nil {
			t.Fatalf("accepted %q", address)
		}
	}
}

func TestInvalidLimiterConfiguration(t *testing.T) {
	for _, cfg := range []Config{{URL: "redis://user:secret@/"}, {URL: "not-redis://secret"}, {URL: "redis://localhost", Namespace: "x\r\ny"}, {URL: "redis://localhost", Timeout: time.Nanosecond}, {URL: "redis://localhost", MaxIdentities: 4097}} {
		l, err := New(cfg)
		if err == nil {
			l.Close()
			t.Fatal("accepted invalid configuration")
		}
		if strings.Contains(err.Error(), "secret") {
			t.Fatal("credential leaked")
		}
	}
	for _, p := range []Policy{{Limit: 0, Window: time.Second}, {Limit: 1, Window: time.Millisecond}, {Limit: 1_000_001, Window: time.Second}, {Limit: 1, Window: time.Second + time.Nanosecond}} {
		if p.Validate() == nil {
			t.Fatal("invalid policy accepted")
		}
	}
}
