package server_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/server"
)

func status(s *server.Server, method, path string) int {
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest(method, path, nil))
	return w.Code
}

func wait(t *testing.T, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(3 * time.Second):
		t.Fatal("HTTP lifecycle event did not occur")
	}
}

type metricsStub struct{}

func (metricsStub) ServeHTTP(w http.ResponseWriter, r *http.Request) { http.NotFound(w, r) }
func (metricsStub) MetricsHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

func TestHealthReadinessAndGracefulDrain(t *testing.T) {
	release, unblock := context.WithCancel(context.Background())
	defer unblock()
	started := make(chan struct{})
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		select {
		case <-release.Done():
			io.WriteString(w, "completed")
		case <-r.Context().Done():
			t.Error("signal canceled in-flight request before drain")
		}
	}))
	defer up.Close()
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	g, err := gateway.New([2]string{up.URL, up.URL}, gateway.Options{Logger: logger})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	s := server.New(g)
	if status(s, "GET", "/healthz") != 200 || status(s, "GET", "/readyz") != 503 || status(s, "GET", "/one") != 503 {
		t.Fatal("startup readiness contract failed")
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- s.Serve(ctx, ln, 2*time.Second, logger) }()
	client := &http.Client{Timeout: 3 * time.Second}
	defer client.CloseIdleConnections()
	base := "http://" + ln.Addr().String()
	for _, tc := range []struct {
		method, path string
		want         int
	}{{"GET", "/healthz", 200}, {"HEAD", "/healthz", 200}, {"GET", "/readyz", 200}, {"POST", "/readyz", 405}} {
		req, _ := http.NewRequest(tc.method, base+tc.path, nil)
		res, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(res.Body)
		res.Body.Close()
		if err != nil || res.StatusCode != tc.want || (tc.method == "HEAD" && len(body) != 0) {
			t.Fatalf("%s %s: status=%d body=%q err=%v", tc.method, tc.path, res.StatusCode, body, err)
		}
	}
	response := make(chan error, 1)
	go func() {
		res, err := client.Get(base + "/one")
		if err == nil {
			var b []byte
			b, err = io.ReadAll(res.Body)
			res.Body.Close()
			if err == nil && (res.StatusCode != 200 || string(b) != "completed") {
				err = io.ErrUnexpectedEOF
			}
		}
		response <- err
	}()
	wait(t, started)
	cancel()
	deadline := time.Now().Add(time.Second)
	for status(s, "GET", "/readyz") != 503 {
		if time.Now().After(deadline) {
			t.Fatal("readiness did not turn false")
		}
		time.Sleep(time.Millisecond)
	}
	if status(s, "GET", "/one") != 503 || status(s, "GET", "/healthz") != 200 {
		t.Fatal("draining admission/liveness failed")
	}
	select {
	case err := <-served:
		t.Fatalf("Serve finished before active request: %v", err)
	default:
	}
	unblock()
	if err := <-response; err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-served:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown did not finish")
	}
	if status(s, "GET", "/readyz") != 503 {
		t.Fatal("ready after shutdown")
	}
}

func TestShutdownDeadlineCancelsActiveUpstream(t *testing.T) {
	started, canceled := make(chan struct{}), make(chan struct{})
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(started); <-r.Context().Done(); close(canceled) }))
	defer up.Close()
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	g, err := gateway.New([2]string{up.URL, up.URL}, gateway.Options{Logger: logger})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	s := server.New(g)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- s.Serve(ctx, ln, 50*time.Millisecond, logger) }()
	client := &http.Client{Timeout: 3 * time.Second}
	defer client.CloseIdleConnections()
	requestDone := make(chan struct{})
	go func() {
		res, _ := client.Get("http://" + ln.Addr().String() + "/one")
		if res != nil {
			res.Body.Close()
		}
		close(requestDone)
	}()
	wait(t, started)
	cancel()
	select {
	case err := <-served:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("shutdown error=%v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("deadline was not bounded")
	}
	wait(t, canceled)
	wait(t, requestDone)
	if status(s, "GET", "/readyz") != 503 {
		t.Fatal("ready after forced shutdown")
	}
}

func TestMetricsBearerAuthDoesNotProtectHealth(t *testing.T) {
	s := server.NewWithMetricsToken(metricsStub{}, "0123456789abcdef0123456789abcdef")
	for _, tc := range []struct {
		auth string
		want int
	}{{"", 401}, {"Bearer wrong", 401}, {"0123456789abcdef0123456789abcdef", 401}, {"Bearer 0123456789abcdef0123456789abcdef", 200}, {"bearer 0123456789abcdef0123456789abcdef", 200}} {
		r := httptest.NewRequest("GET", "/metrics", nil)
		if tc.auth != "" {
			r.Header.Set("Authorization", tc.auth)
		}
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("auth %q: status %d, want %d", tc.auth, w.Code, tc.want)
		}
	}
	if got := status(s, "GET", "/healthz"); got != 200 {
		t.Fatalf("health status %d", got)
	}
}
