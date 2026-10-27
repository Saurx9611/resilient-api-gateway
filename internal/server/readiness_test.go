package server_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/ratelimit"
	"example.com/resilient-api-gateway/internal/server"
)

type dependency struct{ down atomic.Bool }

func (d *dependency) Check(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error) {
	if d.down.Load() {
		return ratelimit.Decision{}, errors.New("dependency down")
	}
	return ratelimit.Decision{Allowed: true, Remaining: 59, Reset: time.Second}, nil
}
func (d *dependency) Health(context.Context) error {
	if d.down.Load() {
		return errors.New("dependency down")
	}
	return nil
}

func TestRedisReadinessModesAndRestoration(t *testing.T) {
	d := new(dependency)
	d.down.Store(true)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer up.Close()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	g, e := gateway.New([2]string{up.URL, up.URL}, gateway.Options{Limiter: d, RedisHealth: d.Health, Logger: logger})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	s := server.New(g)
	if status(s, "GET", "/readyz") != 503 || status(s, "GET", "/healthz") != 200 {
		t.Fatal("startup must not be ready")
	}
	ln, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.Serve(ctx, ln, time.Second, logger) }()
	client := &http.Client{Timeout: 2 * time.Second}
	defer client.CloseIdleConnections()
	base := "http://" + ln.Addr().String()
	expect := func(path string, want int) {
		t.Helper()
		res, e := client.Get(base + path)
		if e != nil {
			t.Fatal(e)
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
		if res.StatusCode != want || (path == "/readyz" && res.Header.Get("Cache-Control") != "no-store") {
			t.Fatalf("%s=%d want %d", path, res.StatusCode, want)
		}
	}
	expect("/healthz", 200)
	expect("/readyz", 503)
	expect("/one", 503)
	cfg := g.CurrentConfig()
	cfg.RedisFailure = [2]string{"open", "open"}
	if g.Reload(cfg) != nil {
		t.Fatal("reload failed")
	}
	expect("/readyz", 503)
	expect("/one", 200)
	cfg.Readiness = "policy"
	if g.Reload(cfg) != nil {
		t.Fatal("reload failed")
	}
	expect("/readyz", 200)
	cfg.RedisFailure[0] = "closed"
	if g.Reload(cfg) != nil {
		t.Fatal("reload failed")
	}
	expect("/readyz", 503)
	expect("/one", 503)
	expect("/two", 200)
	d.down.Store(false)
	expect("/readyz", 200)
	expect("/one", 200)
	d.down.Store(true)
	expect("/readyz", 503)
	d.down.Store(false)
	expect("/readyz", 200)
	cancel()
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	if status(s, "GET", "/readyz") != 503 || status(s, "GET", "/healthz") != 200 {
		t.Fatal("drain readiness precedence")
	}
}

func TestReadinessHealthDeadlineAndMissingProbe(t *testing.T) {
	d := new(dependency)
	g, e := gateway.New([2]string{"http://127.0.0.1:1", "http://127.0.0.1:2"}, gateway.Options{Limiter: d})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	if g.Ready(context.Background()) == nil {
		t.Fatal("missing health callback reported ready")
	}
	g, e = gateway.New([2]string{"http://127.0.0.1:1", "http://127.0.0.1:2"}, gateway.Options{Limiter: d, RedisHealth: func(ctx context.Context) error { <-ctx.Done(); return ctx.Err() }})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	start := time.Now()
	if g.Ready(ctx) == nil || time.Since(start) > 300*time.Millisecond {
		t.Fatal("health ignored caller deadline")
	}
}

func TestDrainWinsOverInProgressReadinessProbe(t *testing.T) {
	d := new(dependency)
	entered := make(chan struct{})
	release := make(chan struct{})
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	g, e := gateway.New([2]string{"http://127.0.0.1:1", "http://127.0.0.1:2"}, gateway.Options{Limiter: d, RedisHealth: func(ctx context.Context) error {
		close(entered)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}})
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
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.Serve(ctx, ln, time.Second, logger) }()
	client := &http.Client{Timeout: time.Second}
	defer client.CloseIdleConnections()
	res, e := client.Get("http://" + ln.Addr().String() + "/healthz")
	if e != nil {
		t.Fatal(e)
	}
	res.Body.Close()
	probed := make(chan int, 1)
	go func() { probed <- status(s, "GET", "/readyz") }()
	wait(t, entered)
	cancel()
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	close(release)
	if got := <-probed; got != 503 {
		t.Fatalf("probe returned %d after draining", got)
	}
}
