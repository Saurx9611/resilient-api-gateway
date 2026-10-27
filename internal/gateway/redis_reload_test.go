package gateway_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"sync"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/ratelimit"
	"example.com/resilient-api-gateway/internal/server"
)

// redisGate forwards to a real Redis, and can close/deny only this test client's
// connections. It neither emulates Redis responses nor changes the shared server.
func redisGate(t *testing.T, backend string) (string, func(bool)) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	online := true
	connections := map[net.Conn]bool{}
	var wg sync.WaitGroup
	set := func(up bool) {
		mu.Lock()
		defer mu.Unlock()
		online = up
		if !up {
			for c := range connections {
				c.Close()
			}
		}
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			front, e := ln.Accept()
			if e != nil {
				return
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer front.Close()
				back, e := net.DialTimeout("tcp", backend, time.Second)
				if e != nil {
					return
				}
				defer back.Close()
				mu.Lock()
				if !online {
					mu.Unlock()
					return
				}
				connections[front] = true
				connections[back] = true
				mu.Unlock()
				defer func() { mu.Lock(); delete(connections, front); delete(connections, back); mu.Unlock() }()
				copied := make(chan struct{})
				go func() { io.Copy(back, front); back.Close(); close(copied) }()
				io.Copy(front, back)
				front.Close()
				back.Close()
				<-copied
			}()
		}
	}()
	t.Cleanup(func() { set(false); ln.Close(); wg.Wait() })
	return ln.Addr().String(), set
}

func TestRealRedisOutageReloadReadinessAndRecovery(t *testing.T) {
	endpoint := os.Getenv("REDIS_TEST_URL")
	if endpoint == "" {
		t.Skip("set REDIS_TEST_URL for real Redis outage/recovery")
	}
	u, e := url.Parse(endpoint)
	if e != nil || u.Scheme != "redis" {
		t.Fatal("this real Redis gate test requires redis:// endpoint")
	}
	address, set := redisGate(t, u.Host)
	u.Host = address
	limiter, e := ratelimit.New(ratelimit.Config{URL: u.String(), Namespace: fmt.Sprintf("reload%d", time.Now().UnixNano()), Timeout: 100 * time.Millisecond})
	if e != nil {
		t.Fatal(e)
	}
	defer limiter.Close()
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "upstream") }))
	defer up.Close()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	opts := gateway.Options{Limiter: limiter, RedisHealth: limiter.Health, Logger: logger, RatePolicies: [2]ratelimit.Policy{{Limit: 3, Window: time.Hour}, {Limit: 3, Window: time.Hour, FailOpen: true}}}
	g, e := gateway.New([2]string{up.URL, up.URL}, opts)
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	s := server.New(g)
	ln, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Serve(ctx, ln, time.Second, logger) }()
	defer func() {
		cancel()
		if e := <-done; e != nil {
			t.Error(e)
		}
	}()
	client := &http.Client{Timeout: 2 * time.Second}
	defer client.CloseIdleConnections()
	base := "http://" + ln.Addr().String()
	request := func(path string, want int) http.Header {
		t.Helper()
		res, e := client.Get(base + path)
		if e != nil {
			t.Fatal(e)
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
		if res.StatusCode != want {
			t.Fatalf("%s status %d want %d", path, res.StatusCode, want)
		}
		return res.Header
	}
	request("/readyz", 200)
	if h := request("/one", 200); h.Get("RateLimit-Remaining") != "2" {
		t.Fatal(h)
	}
	// Reload to another valid target preserves stable route quota. In this case
	// both origins share one controlled service, but the operator spelling changes.
	cfg := g.CurrentConfig()
	cfg.Targets[0] = up.URL + "/"
	if e := g.Reload(cfg); e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 3; i++ {
		request("/readyz", 200)
	} // PING must not consume quota
	set(false)
	request("/healthz", 200)
	request("/readyz", 503)
	if h := request("/one", 503); h.Get("RateLimit-Limit") != "" {
		t.Fatal("outage claimed quota", h)
	}
	if h := request("/two", 200); h.Get("RateLimit-Limit") != "" {
		t.Fatal("fail-open claimed quota", h)
	}
	// Readiness reload can explicitly tolerate Redis only when all routes fail open.
	cfg.Readiness = "policy"
	if e := g.Reload(cfg); e != nil {
		t.Fatal(e)
	}
	request("/readyz", 503)
	cfg.RedisFailure[0] = "open"
	if e := g.Reload(cfg); e != nil {
		t.Fatal(e)
	}
	request("/readyz", 200)
	cfg.Readiness = "required"
	if e := g.Reload(cfg); e != nil {
		t.Fatal(e)
	}
	request("/readyz", 503)
	set(true)
	request("/readyz", 200)
	if h := request("/one", 200); h.Get("RateLimit-Remaining") != "1" {
		t.Fatal("reload/readiness reset or consumed quota", h)
	}
	request("/one", 200)
	request("/one", 429)
	cfg.RedisFailure[0] = "closed"
	if e := g.Reload(cfg); e != nil {
		t.Fatal(e)
	}
	if s := g.AdmissionStats(); s[0].FailClosed != 1 || s[1].FailOpen != 1 {
		t.Fatal("missing outage counters", s)
	}
	t.Log("real Redis: required/policy readiness, fail-open/closed requests, restore without restart, quota retained across reload and health probes; final quota rejection 429")
}
