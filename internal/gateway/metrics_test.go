package gateway_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/server"
)

type lockedBuffer struct {
	mu sync.Mutex
	b  strings.Builder
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.Write(p)
}
func (b *lockedBuffer) String() string { b.mu.Lock(); defer b.mu.Unlock(); return b.b.String() }

func TestMetricsAndRequestID(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	s := httptest.NewServer(g)
	defer s.Close()
	for _, tc := range []struct{ path, id string }{{"/one/customer-001?token=secret-value", "chosen-request-0001"}, {"/one/customer-002", "chosen-request-0002"}} {
		req, _ := http.NewRequest(http.MethodGet, s.URL+tc.path, nil)
		req.Header.Set("X-Request-ID", tc.id)
		res, err := s.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		id := res.Header.Get("X-Request-ID")
		res.Body.Close()
		if id != tc.id {
			t.Fatalf("request ID = %q, want %q", id, tc.id)
		}
	}
	var body string
	for i := 0; i < 100; i++ {
		w := httptest.NewRecorder()
		server.New(g).ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/metrics", nil))
		body = w.Body.String()
		if strings.Contains(body, `gateway_requests_total{route="one",outcome="2xx"} 2`) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	for _, want := range []string{"gateway_requests_total{route=\"one\",outcome=\"2xx\"} 2", "gateway_request_duration_seconds_sum", "gateway_upstream_roundtrip_duration_seconds_count", "gateway_circuit_state{route=\"one\",state=\"closed\"} 1"} {
		if !strings.Contains(body, want) {
			t.Fatalf("metrics missing %q: %s", want, body)
		}
	}
	for _, sensitive := range []string{"customer-001", "customer-002", "secret-value", "chosen-request-0001", "chosen-request-0002"} {
		if strings.Contains(body, sensitive) {
			t.Fatalf("user-derived metric dimension %q: %s", sensitive, body)
		}
	}
}

func TestRequestIDSanitizesClientValue(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) {})
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	r := httptest.NewRequest(http.MethodGet, "/one", nil)
	r.Header.Set("X-Request-ID", "bad\r\nInjected: yes")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Header().Get("Injected") != "" || len(w.Header().Get("X-Request-ID")) != 32 {
		t.Fatalf("unsafe request ID handling: %v", w.Header())
	}
}

func TestMetricsHistogramCountsBeyondLargestBucket(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(510 * time.Millisecond)
		w.WriteHeader(http.StatusNoContent)
	})
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	s := httptest.NewServer(g)
	defer s.Close()
	res, err := s.Client().Get(s.URL + "/one/slow")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	var body string
	for i := 0; i < 100; i++ {
		w := httptest.NewRecorder()
		server.New(g).ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/metrics", nil))
		body = w.Body.String()
		if strings.Contains(body, `gateway_request_duration_seconds_count{route="one"} 1`) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !strings.Contains(body, `gateway_request_duration_seconds_bucket{route="one",le="+Inf"} 1`) || !strings.Contains(body, `gateway_upstream_roundtrip_duration_seconds_bucket{route="one",le="+Inf"} 1`) {
		t.Fatalf("overflow observations missing from +Inf: %s", body)
	}
}
