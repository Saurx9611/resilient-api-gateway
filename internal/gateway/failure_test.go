package gateway_test

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
)

func TestUpstreamFailureMapping(t *testing.T) {
	for _, tc := range []struct {
		name       string
		opts       gateway.Options
		disconnect bool
		status     int
	}{
		{"disconnect", gateway.Options{}, true, 502},
		{"response-header-timeout", gateway.Options{ResponseHeaderTimeout: 80 * time.Millisecond, RequestTimeout: 2 * time.Second}, false, 504},
		{"request-deadline", gateway.Options{ResponseHeaderTimeout: 2 * time.Second, RequestTimeout: 80 * time.Millisecond}, false, 504},
	} {
		t.Run(tc.name, func(t *testing.T) {
			canceled := make(chan struct{})
			u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
				if tc.disconnect {
					conn, _, err := w.(http.Hijacker).Hijack()
					if err == nil {
						conn.Close()
					}
					return
				}
				<-r.Context().Done()
				close(canceled)
			})
			var log lockedBuffer
			opts := tc.opts
			opts.Logger = slog.New(slog.NewJSONHandler(&log, nil))
			p := startProxy(t, [2]string{u.URL, u.URL}, opts)
			res, err := p.Client().Get(p.URL + "/one?token=secret")
			if err != nil {
				t.Fatal(err)
			}
			body := readBody(t, res)
			if res.StatusCode != tc.status || body != http.StatusText(tc.status)+"\n" {
				t.Fatalf("response = %d %q", res.StatusCode, body)
			}
			if strings.Contains(log.String(), "secret") || strings.Contains(log.String(), u.URL) {
				t.Fatalf("sensitive logs = %s", log.String())
			}
			if !tc.disconnect {
				await(t, canceled)
			}
		})
	}
}

func TestUpstreamStatusAndRedirectPreserved(t *testing.T) {
	var hit atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		hit.Add(1)
		if r.URL.Path == "/redirect" {
			w.Header().Set("Location", "http://uncontacted.invalid/private")
			w.WriteHeader(302)
			return
		}
		w.WriteHeader(503)
		io.WriteString(w, "upstream unavailable")
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	for _, tc := range []struct {
		path   string
		status int
	}{{"/one/redirect", 302}, {"/two/unavailable", 503}} {
		res, err := p.Client().Get(p.URL + tc.path)
		if err != nil {
			t.Fatal(err)
		}
		readBody(t, res)
		if res.StatusCode != tc.status {
			t.Fatalf("status=%d", res.StatusCode)
		}
		if tc.status == 302 && res.Header.Get("Location") != "http://uncontacted.invalid/private" {
			t.Fatal("redirect changed")
		}
	}
	if hit.Load() != 2 {
		t.Fatalf("unexpected upstream requests: %d", hit.Load())
	}
}

func TestUpstreamTLSVerification(t *testing.T) {
	u := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("untrusted TLS upstream reached handler") }))
	defer u.Close()
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != 502 {
		t.Fatalf("TLS failure status=%d", res.StatusCode)
	}
}

func TestSharedConnectionBudgetAndReuse(t *testing.T) {
	release, unblock := context.WithCancel(context.Background())
	defer unblock()
	entered := make(chan struct{}, 8)
	var active, maxActive atomic.Int32
	var mu sync.Mutex
	peers := map[string]bool{}
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		n := active.Add(1)
		defer active.Add(-1)
		for old := maxActive.Load(); n > old && !maxActive.CompareAndSwap(old, n); old = maxActive.Load() {
		}
		mu.Lock()
		peers[r.RemoteAddr] = true
		mu.Unlock()
		entered <- struct{}{}
		select {
		case <-release.Done():
		case <-r.Context().Done():
			return
		}
		io.WriteString(w, "ok")
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{MaxConnsPerHost: 1})
	done := make(chan error, 8)
	request := func(path string) {
		res, err := p.Client().Get(p.URL + path)
		if err == nil {
			_, err = io.Copy(io.Discard, res.Body)
			res.Body.Close()
		}
		done <- err
	}
	go request("/one")
	await(t, entered)
	for i := 1; i < 8; i++ {
		go request("/two")
	}
	select {
	case <-entered:
		t.Fatal("second request exceeded shared connection budget")
	case <-time.After(75 * time.Millisecond):
	}
	unblock()
	for i := 0; i < 8; i++ {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	mu.Lock()
	count := len(peers)
	mu.Unlock()
	if maxActive.Load() != 1 || count != 1 {
		t.Fatalf("active max=%d; upstream connections=%d", maxActive.Load(), count)
	}
}

func TestUpstreamResponseHeaderBudget(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Large", strings.Repeat("x", 40<<10))
		io.WriteString(w, "not forwarded")
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != 502 {
		t.Fatalf("oversized header status = %d", res.StatusCode)
	}
}

func TestPostIsNotRetriedAfterUpstreamDisconnect(t *testing.T) {
	var hits atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) {
		hits.Add(1)
		conn, _, err := w.(http.Hijacker).Hijack()
		if err == nil {
			_ = conn.Close()
		}
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	res, err := p.Client().Post(p.URL+"/one/charge", "application/octet-stream", strings.NewReader("one-time-effect"))
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != http.StatusBadGateway || hits.Load() != 1 {
		t.Fatalf("POST retry/status: hits=%d status=%d", hits.Load(), res.StatusCode)
	}
}
