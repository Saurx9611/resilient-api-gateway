package gateway_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/ratelimit"
)

func TestRealRedisConnectionLossPolicies(t *testing.T) {
	endpoint := os.Getenv("REDIS_TEST_URL")
	if endpoint == "" {
		t.Skip("set REDIS_TEST_URL")
	}
	limiter, err := ratelimit.New(ratelimit.Config{URL: endpoint, Namespace: fmt.Sprintf("outage%d", time.Now().UnixNano())})
	if err != nil {
		t.Fatal(err)
	}
	defer limiter.Close()
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{Limiter: limiter, RatePolicies: [2]ratelimit.Policy{ratelimit.DefaultPolicy(), {Limit: 60, Window: time.Minute, FailOpen: true}}})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != 200 {
		t.Fatal(res.StatusCode)
	}
	// Closing the actual Redis client makes established Redis access unavailable.
	limiter.Close()
	for i, want := range []int{503, 200} {
		res, err := p.Client().Get(p.URL + []string{"/one", "/two"}[i])
		if err != nil {
			t.Fatal(err)
		}
		readBody(t, res)
		if res.StatusCode != want || res.Header.Get("RateLimit-Limit") != "" {
			t.Fatalf("outage=%d %v", res.StatusCode, res.Header)
		}
	}
	stats := g.AdmissionStats()
	if stats[0].FailClosed != 1 || stats[1].FailOpen != 1 || stats[0].RedisErrors != 1 || stats[1].RedisErrors != 1 {
		t.Fatal(stats)
	}
}

type admissionFunc func(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error)

func (f admissionFunc) Check(ctx context.Context, route, id string, p ratelimit.Policy) (ratelimit.Decision, error) {
	return f(ctx, route, id, p)
}

func TestAdmissionHeadersAndNoBodyRead(t *testing.T) {
	var calls atomic.Int32
	limiter := admissionFunc(func(ctx context.Context, route, id string, p ratelimit.Policy) (ratelimit.Decision, error) {
		calls.Add(1)
		want, _ := ratelimit.PeerID("192.0.2.1:1")
		if route != "one" || id != want {
			t.Errorf("untrusted route/identity: %q %q", route, id)
		}
		return ratelimit.Decision{Remaining: 0, Reset: 1501 * time.Millisecond, RetryAfter: 1001 * time.Millisecond}, nil
	})
	g, err := gateway.New([2]string{"http://127.0.0.1:1", "http://127.0.0.1:2"}, gateway.Options{Limiter: limiter})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	body := new(unreadBody)
	req := httptest.NewRequest("POST", "/one/upload", nil)
	req.RemoteAddr = "192.0.2.1:4567"
	req.Body = body
	req.ContentLength = 1_000_000
	req.Header.Set("X-Forwarded-For", "198.51.100.4")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, req)
	if w.Code != 429 || body.read || w.Header().Get("RateLimit-Limit") != "60" || w.Header().Get("RateLimit-Remaining") != "0" || w.Header().Get("RateLimit-Reset") != "2" || w.Header().Get("Retry-After") != "2" || w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("Connection") != "close" {
		t.Fatalf("rejection %d, headers %v, read=%v", w.Code, w.Header(), body.read)
	}
	if calls.Load() != 1 || g.AdmissionStats()[0].Limited != 1 {
		t.Fatal("admission counters")
	}
	for _, path := range []string{"/unknown", "/one/%2e%2e/x"} {
		r := httptest.NewRequest("POST", path, nil)
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
	}
	if calls.Load() != 1 {
		t.Fatal("invalid routing consumed quota")
	}
}

func TestAdmissionOutagePoliciesAndHooks(t *testing.T) {
	var upstreamHits atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		upstreamHits.Add(1)
		w.Header().Set("RateLimit-Limit", "999999")
		w.Header().Set("Cache-Control", "public")
		io.WriteString(w, "ok")
	})
	var logs lockedBuffer
	logger := slog.New(slog.NewJSONHandler(&logs, nil))
	events := make(chan gateway.AdmissionEvent, 4)
	limiter := admissionFunc(func(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error) {
		return ratelimit.Decision{}, errors.New("redis://user:secret@private")
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{Logger: logger, Limiter: limiter, RatePolicies: [2]ratelimit.Policy{ratelimit.DefaultPolicy(), {Limit: 60, Window: time.Minute, FailOpen: true}}, ObserveAdmission: func(e gateway.AdmissionEvent) { events <- e }})
	for _, tc := range []struct {
		path    string
		status  int
		outcome string
	}{{"/one", 503, "fail_closed"}, {"/two", 200, "fail_open"}} {
		res, err := p.Client().Get(p.URL + tc.path)
		if err != nil {
			t.Fatal(err)
		}
		readBody(t, res)
		if res.StatusCode != tc.status || res.Header.Get("RateLimit-Limit") != "" || res.Header.Get("RateLimit-Remaining") != "" || res.Header.Get("Cache-Control") != "no-store" {
			t.Fatalf("outage response %d %v", res.StatusCode, res.Header)
		}
		if event := <-events; event.Outcome != tc.outcome || event.Elapsed < 0 || strings.Contains(event.Route, "/") {
			t.Fatalf("event=%+v", event)
		}
	}
	if upstreamHits.Load() != 1 || strings.Contains(logs.String(), "secret") || strings.Contains(logs.String(), "private") {
		t.Fatalf("hits/logs=%d %s", upstreamHits.Load(), logs.String())
	}
}

func TestAdmissionSuccessOwnsHeadersAndConsumesOnce(t *testing.T) {
	var calls atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("RateLimit-Limit", "999")
		w.Header().Set("RateLimit-Remaining", "999")
		w.Header().Set("RateLimit-Reset", "999")
		w.WriteHeader(500)
	})
	limiter := admissionFunc(func(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error) {
		calls.Add(1)
		return ratelimit.Decision{Allowed: true, Remaining: 59, Reset: time.Minute}, nil
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{Limiter: limiter})
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != 500 || res.Header.Get("RateLimit-Limit") != "60" || res.Header.Get("RateLimit-Remaining") != "59" || len(res.Header.Values("RateLimit-Limit")) != 1 || calls.Load() != 1 {
		t.Fatalf("upstream overwrite/refund: %d %v calls=%d", res.StatusCode, res.Header, calls.Load())
	}
}

func TestInvalidPeerNeverFailsOpen(t *testing.T) {
	limiter := admissionFunc(func(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error) {
		t.Fatal("invalid identity reached Redis")
		return ratelimit.Decision{}, nil
	})
	g, err := gateway.New([2]string{"http://127.0.0.1:1", "http://127.0.0.1:2"}, gateway.Options{Limiter: limiter, RatePolicies: [2]ratelimit.Policy{{Limit: 60, Window: time.Minute, FailOpen: true}, ratelimit.DefaultPolicy()}})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	r := httptest.NewRequest("GET", "/one", nil)
	r.RemoteAddr = "spoofed"
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 400 || g.AdmissionStats()[0].InvalidIdentity != 1 {
		t.Fatalf("invalid peer status=%d", w.Code)
	}
}

func TestCanceledAdmissionCannotFailOpen(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	limiter := admissionFunc(func(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error) {
		cancel()
		return ratelimit.Decision{}, context.Canceled
	})
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { t.Error("canceled admission was proxied") })
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{Limiter: limiter, RatePolicies: [2]ratelimit.Policy{{Limit: 60, Window: time.Minute, FailOpen: true}, ratelimit.DefaultPolicy()}})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	r := httptest.NewRequest("GET", "/one", nil).WithContext(ctx)
	g.ServeHTTP(httptest.NewRecorder(), r)
	if g.AdmissionStats()[0].FailOpen != 0 {
		t.Fatal("cancellation counted as fail-open")
	}
}
