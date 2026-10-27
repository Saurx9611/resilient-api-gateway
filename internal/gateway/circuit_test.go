package gateway_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
	"example.com/resilient-api-gateway/internal/gateway"
)

// A client can observe an empty response's headers/EOF before the handler has
// finished its body-copy cleanup. Wait for the server-side permit completion.
func circuitIdle(t *testing.T, g *gateway.Gateway) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if g.CircuitStats()[0].ActiveProbes == 0 {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("circuit permit did not complete", g.CircuitStats())
}

func TestCircuitHTTPTransitionsAndIsolation(t *testing.T) {
	var code atomic.Int32
	code.Store(500)
	var hits atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { hits.Add(1); w.WriteHeader(int(code.Load())) })
	other := upstream(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	var nanos atomic.Int64
	nanos.Store(time.Now().UnixNano())
	g, e := gateway.New([2]string{u.URL, other.URL}, gateway.Options{CircuitPolicy: breaker.Policy{FailureThreshold: 2, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 2}, CircuitNow: func() time.Time { return time.Unix(0, nanos.Load()) }})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	get := func(path string, want int) {
		t.Helper()
		res, e := p.Client().Get(p.URL + path)
		if e != nil {
			t.Fatal(e)
		}
		readBody(t, res)
		if res.StatusCode != want {
			t.Fatalf("%s status=%d want %d", path, res.StatusCode, want)
		}
	}
	get("/one", 500)
	code.Store(404)
	get("/one", 404)
	code.Store(500)
	get("/one", 500)
	if g.CircuitStats()[0].State != breaker.Open {
		t.Fatal(g.CircuitStats())
	}
	before := hits.Load()
	body := new(unreadBody)
	req := httptest.NewRequest("POST", "/one", nil)
	req.Body = body
	req.ContentLength = 1_000_000
	w := httptest.NewRecorder()
	start := time.Now()
	g.ServeHTTP(w, req)
	if w.Code != 503 || body.read || hits.Load() != before || w.Header().Get("Retry-After") != "1" || w.Header().Get("Cache-Control") != "no-store" || time.Since(start) > 100*time.Millisecond {
		t.Fatalf("open response %d %v read=%v", w.Code, w.Header(), body.read)
	}
	get("/two", 200)
	nanos.Add(int64(time.Second))
	code.Store(200)
	get("/one", 200)
	circuitIdle(t, g)
	if g.CircuitStats()[0].State != breaker.HalfOpen {
		t.Fatal("closed before recovery count")
	}
	get("/one", 200)
	circuitIdle(t, g)
	if g.CircuitStats()[0].State != breaker.Closed {
		t.Fatal(g.CircuitStats())
	}
}

func TestCircuitSharedOriginAndTransportFailure(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })
	g, e := gateway.New([2]string{u.URL, u.URL + "/"}, gateway.Options{CircuitPolicy: breaker.Policy{FailureThreshold: 1, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 1}})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	res, e := p.Client().Get(p.URL + "/one")
	if e != nil {
		t.Fatal(e)
	}
	readBody(t, res)
	if s := g.CircuitStats(); s[0].State != breaker.Open || s[1].State != breaker.Open {
		t.Fatal("origin not shared", s)
	}
	res, e = p.Client().Get(p.URL + "/two")
	if e != nil {
		t.Fatal(e)
	}
	readBody(t, res)
	if res.StatusCode != 503 || g.CircuitStats()[0].Rejected != 1 {
		t.Fatal("shared route not rejected")
	}
	// A genuinely disconnected configured server maps to 502 then opens.
	dead := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	dead.Close()
	q := startProxy(t, [2]string{dead.URL, u.URL}, gateway.Options{CircuitPolicy: breaker.Policy{FailureThreshold: 1, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 1}})
	for _, want := range []int{502, 503} {
		res, e := q.Client().Get(q.URL + "/one")
		if e != nil {
			t.Fatal(e)
		}
		readBody(t, res)
		if res.StatusCode != want {
			t.Fatalf("transport status %d want %d", res.StatusCode, want)
		}
	}
}

func TestCircuitConcurrentHTTPProbes(t *testing.T) {
	var healthy atomic.Bool
	entered := make(chan struct{}, 8)
	release := make(chan struct{})
	defer close(release)
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		if !healthy.Load() {
			w.WriteHeader(500)
			return
		}
		entered <- struct{}{}
		select {
		case <-release:
		case <-r.Context().Done():
		}
		io.WriteString(w, "ok")
	})
	var now atomic.Int64
	now.Store(time.Now().UnixNano())
	g, e := gateway.New([2]string{u.URL, u.URL}, gateway.Options{CircuitPolicy: breaker.Policy{FailureThreshold: 1, OpenDuration: time.Second, MaxProbes: 2, RecoverySuccesses: 2}, CircuitNow: func() time.Time { return time.Unix(0, now.Load()) }})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	res, e := p.Client().Get(p.URL + "/one")
	if e != nil {
		t.Fatal(e)
	}
	readBody(t, res)
	healthy.Store(true)
	now.Add(int64(time.Second))
	done := make(chan int, 20)
	for i := 0; i < 2; i++ {
		go func() {
			res, e := p.Client().Get(p.URL + "/one")
			if e != nil {
				done <- 0
				return
			}
			io.Copy(io.Discard, res.Body)
			res.Body.Close()
			done <- res.StatusCode
		}()
	}
	for i := 0; i < 2; i++ {
		select {
		case <-entered:
		case <-time.After(3 * time.Second):
			t.Fatal("probe missing")
		}
	}
	for i := 0; i < 18; i++ {
		go func() {
			res, e := p.Client().Get(p.URL + "/one")
			if e != nil {
				done <- 0
				return
			}
			res.Body.Close()
			done <- res.StatusCode
		}()
	}
	for i := 0; i < 18; i++ {
		if status := <-done; status != 503 {
			t.Fatal("probe escaped cap", status)
		}
	}
	if s := g.CircuitStats()[0]; s.ActiveProbes != 2 || s.Rejected != 18 {
		t.Fatal(s)
	}
	// Cancel waiting upstreams by releasing them, then assert deterministic closure.
	release <- struct{}{}
	release <- struct{}{}
	for i := 0; i < 2; i++ {
		if status := <-done; status != 200 {
			t.Fatal(status)
		}
	}
	circuitIdle(t, g)
	if g.CircuitStats()[0].State != breaker.Closed {
		t.Fatal(g.CircuitStats())
	}
}

func TestCircuitClientCancellationNeutral(t *testing.T) {
	entered := make(chan struct{})
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { close(entered); <-r.Context().Done() })
	g, e := gateway.New([2]string{u.URL, u.URL}, gateway.Options{CircuitPolicy: breaker.Policy{FailureThreshold: 1, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 1}})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r := httptest.NewRequest("GET", "/one", nil).WithContext(ctx)
	done := make(chan struct{})
	go func() { g.ServeHTTP(httptest.NewRecorder(), r); close(done) }()
	<-entered
	cancel()
	<-done
	if s := g.CircuitStats()[0]; s.State != breaker.Closed || s.Failures != 0 || s.Neutral != 1 {
		t.Fatal(s)
	}
}

func TestCircuitStreamedFailureKeepsProbeSlot(t *testing.T) {
	release, unblock := context.WithCancel(context.Background())
	defer unblock()
	var stream atomic.Bool
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		if stream.Load() {
			io.WriteString(w, "first")
			w.(http.Flusher).Flush()
			select {
			case <-release.Done():
			case <-r.Context().Done():
			}
		}
	})
	var now atomic.Int64
	now.Store(time.Now().UnixNano())
	g, e := gateway.New([2]string{u.URL, u.URL}, gateway.Options{CircuitPolicy: breaker.Policy{FailureThreshold: 1, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 1}, CircuitNow: func() time.Time { return time.Unix(0, now.Load()) }})
	if e != nil {
		t.Fatal(e)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	defer unblock()
	res, e := p.Client().Get(p.URL + "/one")
	if e != nil {
		t.Fatal(e)
	}
	readBody(t, res)
	stream.Store(true)
	now.Add(int64(time.Second))
	probe, e := p.Client().Get(p.URL + "/one")
	if e != nil {
		t.Fatal(e)
	}
	defer probe.Body.Close()
	if s := g.CircuitStats()[0]; s.State != breaker.HalfOpen || s.ActiveProbes != 1 {
		t.Fatal("failure headers released streaming permit", s)
	}
	res, e = p.Client().Get(p.URL + "/one")
	if e != nil {
		t.Fatal(e)
	}
	readBody(t, res)
	if res.StatusCode != 503 {
		t.Fatal(res.StatusCode)
	}
	unblock()
	readBody(t, probe)
	circuitIdle(t, g)
	if g.CircuitStats()[0].State != breaker.Open {
		t.Fatal("failed probe did not reopen", g.CircuitStats())
	}
}
