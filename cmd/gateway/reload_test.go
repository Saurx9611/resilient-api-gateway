package main

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
)

func TestWatchConfigInvalidMissingAndRestoration(t *testing.T) {
	initial := []byte(`{"targets":["http://127.0.0.1:1","http://127.0.0.1:2"],"redis_failure":["closed","closed"]}`)
	cfg, e := gateway.DecodeConfig(initial)
	if e != nil {
		t.Fatal(e)
	}
	g, e := gateway.NewConfigured(cfg, gateway.Options{})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	path := filepath.Join(t.TempDir(), "routes.json")
	write := func(data []byte) {
		t.Helper()
		if e := os.WriteFile(path+".tmp", data, 0600); e != nil {
			t.Fatal(e)
		}
		if e := replaceTestFile(path+".tmp", path); e != nil {
			t.Fatal(e)
		}
	}
	write(initial)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		watchConfig(ctx, path, 5*time.Millisecond, g, initial, slog.New(slog.NewTextHandler(io.Discard, nil)))
	}()
	until := func(check func() bool) {
		t.Helper()
		deadline := time.Now().Add(2 * time.Second)
		for !check() {
			if time.Now().After(deadline) {
				t.Fatal("watcher did not apply expected state", g.ReloadStats())
			}
			time.Sleep(time.Millisecond)
		}
	}
	write([]byte(`{"targets":["http://user:secret@host"]}`))
	until(func() bool { return g.ReloadStats().Rejected >= 1 })
	before := g.ReloadStats()
	time.Sleep(25 * time.Millisecond)
	if g.ReloadStats() != before {
		t.Fatal("unchanged invalid bytes repeatedly processed")
	}
	if e := os.Remove(path); e != nil {
		t.Fatal(e)
	}
	until(func() bool { return g.ReloadStats().Rejected > before.Rejected })
	if g.CurrentConfig() != cfg {
		t.Fatal("missing file changed state")
	}
	next := []byte(strings.Replace(string(initial), "127.0.0.1:1", "127.0.0.1:3", 1))
	write(next)
	until(func() bool { return g.ReloadStats().Version == 2 })
	if g.CurrentConfig().Targets[0] != "http://127.0.0.1:3" {
		t.Fatal(g.CurrentConfig())
	}
	write(initial)
	until(func() bool { return g.ReloadStats().Version == 3 })
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("watcher leaked")
	}
}
