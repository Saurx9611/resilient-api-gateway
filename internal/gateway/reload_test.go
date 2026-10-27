package gateway_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/ratelimit"
)

func reloadConfig(one, two string) gateway.Config {
	return gateway.Config{Targets: [2]string{one, two}, RedisFailure: [2]string{"closed", "closed"}, Readiness: "required"}
}
func quietOptions() gateway.Options {
	return gateway.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
}

func TestReloadDuringConcurrentLongStreams(t *testing.T) {
	release, unblock := context.WithCancel(context.Background())
	defer unblock()
	entered := make(chan struct{}, 16)
	old := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "old:")
		w.(http.Flusher).Flush()
		entered <- struct{}{}
		select {
		case <-release.Done():
			io.WriteString(w, "finished")
		case <-r.Context().Done():
			t.Error("reload canceled old request")
		}
	})
	fresh := upstream(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "new") })
	g, err := gateway.New([2]string{old.URL, old.URL}, quietOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	defer unblock()
	results := make(chan error, 16)
	for i := 0; i < 16; i++ {
		go func() {
			res, e := p.Client().Get(p.URL + "/one/hold")
			if e == nil {
				data, re := io.ReadAll(res.Body)
				res.Body.Close()
				e = re
				if e == nil && string(data) != "old:finished" {
					e = fmt.Errorf("old response %q", data)
				}
			}
			results <- e
		}()
	}
	for i := 0; i < 16; i++ {
		select {
		case <-entered:
		case <-time.After(3 * time.Second):
			t.Fatal("old stream not entered")
		}
	}
	if err := g.Reload(reloadConfig(fresh.URL, fresh.URL)); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				cfg := g.CurrentConfig()
				if cfg.Targets[0] != cfg.Targets[1] {
					t.Error("partially published routes")
				}
				g.CircuitStats()
				g.AdmissionStats()
			}
		}()
	}
	for i := 0; i < 20; i++ {
		cfg := reloadConfig(fresh.URL, fresh.URL)
		if i%2 == 0 {
			cfg.RedisFailure = [2]string{"open", "open"}
		}
		if e := g.Reload(cfg); e != nil {
			t.Fatal(e)
		}
		res, e := p.Client().Get(p.URL + "/two")
		if e != nil {
			t.Fatal(e)
		}
		if got := readBody(t, res); got != "new" {
			t.Fatal(got)
		}
	}
	close(stop)
	wg.Wait()
	// An invalid second target must not expose the otherwise-valid first route.
	before := g.ReloadStats()
	cfg := reloadConfig(old.URL, "http://user:secret@invalid")
	if err := g.Reload(cfg); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatal("invalid target accepted/leaked", err)
	}
	after := g.ReloadStats()
	if after.Version != before.Version || after.Rejected != before.Rejected+1 || g.CurrentConfig().Targets[0] != fresh.URL {
		t.Fatal("last good snapshot lost", after)
	}
	unblock()
	for i := 0; i < 16; i++ {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	// Restoration may deliberately select the previous target; previously selected
	// traffic already completed, and future traffic sees the restored snapshot.
	if err := g.Reload(reloadConfig(old.URL, old.URL)); err != nil {
		t.Fatal(err)
	}
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	if got := readBody(t, res); got != "old:finished" {
		t.Fatal(got)
	}
	t.Log("16 old streams completed across 21 valid swaps; new traffic used new target; invalid candidate retained last good version; restoration passed")
}

func TestReloadRetainsSelectedAdmissionPolicy(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	limiter := admissionFunc(func(ctx context.Context, _ string, _ string, _ ratelimit.Policy) (ratelimit.Decision, error) {
		if calls.Add(1) == 1 {
			close(entered)
			select {
			case <-release:
			case <-ctx.Done():
			}
		}
		return ratelimit.Decision{}, errors.New("dependency unavailable")
	})
	old := upstream(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "old") })
	fresh := upstream(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "new") })
	opts := quietOptions()
	opts.Limiter = limiter
	g, err := gateway.New([2]string{old.URL, old.URL}, opts)
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	first := make(chan int, 1)
	go func() {
		res, e := p.Client().Get(p.URL + "/one")
		if e != nil {
			first <- 0
			return
		}
		res.Body.Close()
		first <- res.StatusCode
	}()
	<-entered
	cfg := reloadConfig(fresh.URL, fresh.URL)
	cfg.RedisFailure = [2]string{"open", "open"}
	if err := g.Reload(cfg); err != nil {
		t.Fatal(err)
	}
	close(release)
	if status := <-first; status != 503 {
		t.Fatal("old request adopted new fail-open mode", status)
	}
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	if got := readBody(t, res); res.StatusCode != 200 || got != "new" {
		t.Fatal(res.StatusCode, got)
	}
	if s := g.AdmissionStats()[0]; s.FailClosed != 1 || s.FailOpen != 1 {
		t.Fatal(s)
	}
}

func TestReloadKeepsOldTargetBeforeRedisCompletes(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	limiter := admissionFunc(func(ctx context.Context, _ string, _ string, _ ratelimit.Policy) (ratelimit.Decision, error) {
		if calls.Add(1) == 1 {
			close(entered)
			select {
			case <-release:
			case <-ctx.Done():
			}
		}
		return ratelimit.Decision{}, errors.New("dependency unavailable")
	})
	old := upstream(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "old") })
	fresh := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("closed request reached new target")
		w.WriteHeader(200)
	})
	opts := quietOptions()
	opts.Limiter = limiter
	opts.RatePolicies[0] = ratelimit.Policy{Limit: 60, Window: time.Minute, FailOpen: true}
	g, e := gateway.New([2]string{old.URL, old.URL}, opts)
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	type result struct {
		code int
		body string
	}
	done := make(chan result, 1)
	go func() {
		res, e := p.Client().Get(p.URL + "/one")
		if e != nil {
			done <- result{}
			return
		}
		body, _ := io.ReadAll(res.Body)
		res.Body.Close()
		done <- result{res.StatusCode, string(body)}
	}()
	<-entered
	if e := g.Reload(reloadConfig(fresh.URL, fresh.URL)); e != nil {
		t.Fatal(e)
	}
	close(release)
	if got := <-done; got.code != 200 || got.body != "old" {
		t.Fatal("request mixed snapshots after Redis wait", got)
	}
	res, e := p.Client().Get(p.URL + "/one")
	if e != nil {
		t.Fatal(e)
	}
	readBody(t, res)
	if res.StatusCode != 503 {
		t.Fatal(res.StatusCode)
	}
}

func TestReloadPreservesCircuitsForRetainedOrigins(t *testing.T) {
	bad := upstream(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) })
	good := upstream(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	opts := quietOptions()
	opts.CircuitPolicy = breaker.Policy{FailureThreshold: 1, OpenDuration: time.Hour, MaxProbes: 1, RecoverySuccesses: 1}
	g, err := gateway.New([2]string{bad.URL, good.URL}, opts)
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	req := httptest.NewRequest("GET", "/one", nil)
	g.ServeHTTP(httptest.NewRecorder(), req)
	if g.CircuitStats()[0].State != breaker.Open {
		t.Fatal("did not open")
	}
	if err := g.Reload(reloadConfig(good.URL, bad.URL+"/")); err != nil {
		t.Fatal(err)
	}
	if s := g.CircuitStats(); s[0].State != breaker.Closed || s[1].State != breaker.Open {
		t.Fatal("moved origin lost state", s)
	}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("GET", "/two", nil))
	if w.Code != 503 {
		t.Fatal(w.Code)
	}
	// Invalid reload cannot reset the retained breaker or advance version.
	version := g.ReloadStats().Version
	cfg := reloadConfig(good.URL, bad.URL)
	cfg.RedisFailure[0] = "typo"
	if g.Reload(cfg) == nil || g.ReloadStats().Version != version || g.CircuitStats()[1].State != breaker.Open {
		t.Fatal("invalid reload changed circuit")
	}
}

func TestConcurrentReloadWritersAndNoop(t *testing.T) {
	g, e := gateway.New([2]string{"http://127.0.0.1:1", "http://127.0.0.1:1"}, quietOptions())
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	cfg := g.CurrentConfig()
	if g.Reload(cfg) != nil || g.ReloadStats().Version != 1 {
		t.Fatal("no-op advanced version")
	}
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			cfg := reloadConfig(fmt.Sprintf("http://127.0.0.1:%d", 1000+i), fmt.Sprintf("http://127.0.0.1:%d", 1000+i))
			if e := g.Reload(cfg); e != nil {
				t.Error(e)
			}
		}(i)
	}
	wg.Wait()
	if s := g.ReloadStats(); s.Applied != 40 || s.Version != 41 {
		t.Fatal(s)
	}
}
