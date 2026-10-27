package gateway_test

import (
	"encoding/json"
	"fmt"
	"io"
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

const testDemoToken = "0123456789abcdef0123456789abcdef"

func TestDemoDisabledWithoutServerTokenAndRejectsWeakToken(t *testing.T) {
	u := upstream(t, func(http.ResponseWriter, *http.Request) {})
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	if w := demoRequest(g, http.MethodGet, "/demo/status", testDemoToken, nil); w.Code != http.StatusNotFound {
		t.Fatalf("demo without token should be disabled, got %d", w.Code)
	}
	if _, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{DemoToken: "too-short"}); err == nil {
		t.Fatal("weak demo token accepted")
	}
}

func demoRequest(g *gateway.Gateway, method, path, token string, body io.Reader) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, body)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	return w
}

func TestDemoControlsRequireAuthorizationAndFixedActions(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) {})
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{DemoToken: testDemoToken})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	for _, tc := range []struct {
		method, path, token string
		body                io.Reader
		want                int
	}{
		{http.MethodGet, "/demo/status", "", nil, http.StatusUnauthorized},
		{http.MethodGet, "/demo/status", "incorrect-token-which-is-long-enough", nil, http.StatusUnauthorized},
		{http.MethodGet, "/demo/status?debug=true", testDemoToken, nil, http.StatusBadRequest},
		{http.MethodGet, "/demo/status", testDemoToken, strings.NewReader("unexpected"), http.StatusRequestEntityTooLarge},
		{http.MethodGet, "/demo/actions/normal", testDemoToken, nil, http.StatusMethodNotAllowed},
		{http.MethodPost, "/demo/actions/other", testDemoToken, nil, http.StatusNotFound},
		{http.MethodPost, "/demo/actions/normal", testDemoToken, strings.NewReader("target=http://evil.invalid"), http.StatusRequestEntityTooLarge},
	} {
		w := demoRequest(g, tc.method, tc.path, tc.token, tc.body)
		if w.Code != tc.want {
			t.Fatalf("%s %s -> %d, want %d", tc.method, tc.path, w.Code, tc.want)
		}
	}
	status := demoRequest(g, http.MethodGet, "/demo/status", testDemoToken, nil)
	var snapshot gateway.DemoStatus
	if status.Code != http.StatusOK || !strings.Contains(status.Body.String(), `"sampled_at"`) {
		t.Fatalf("status snapshot %d %s", status.Code, status.Body.String())
	}
	if err := json.Unmarshal(status.Body.Bytes(), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Routes[0].RequestCount != 0 || snapshot.Routes[0].AverageDurationSeconds != nil || snapshot.Routes[0].AverageUpstreamSeconds != nil || snapshot.Routes[0].AverageRedisSeconds != nil {
		t.Fatalf("empty sample should not invent timing values: %+v", snapshot.Routes[0])
	}
}

func TestDemoCircuitRecoveryUsesIsolatedBreaker(t *testing.T) {
	var upstreamHits atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) { upstreamHits.Add(1); w.WriteHeader(http.StatusOK) })
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{DemoToken: testDemoToken})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	w := demoRequest(g, http.MethodPost, "/demo/actions/circuit-recovery", testDemoToken, nil)
	if w.Code != http.StatusOK {
		t.Fatalf("action=%d %s", w.Code, w.Body.String())
	}
	var result gateway.DemoActionResult
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Circuit == nil || !result.Circuit.Isolated || len(result.Circuit.Steps) != 5 || upstreamHits.Load() != 0 {
		t.Fatalf("circuit simulation=%+v upstream hits=%d", result.Circuit, upstreamHits.Load())
	}
	steps := result.Circuit.Steps
	if steps[1].State != "open" || steps[2].GatewayStatus != http.StatusServiceUnavailable || steps[3].State != "half_open" || steps[4].State != "closed" {
		t.Fatalf("unexpected FSM sequence: %+v", steps)
	}
}

func TestDemoNormalIsFixedGatewayRequestAndCooldownApplies(t *testing.T) {
	var hits atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if r.URL.Path != "/__demo/normal" {
			t.Errorf("unexpected demo path %q", r.URL.Path)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{DemoToken: testDemoToken})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	w := demoRequest(g, http.MethodPost, "/demo/actions/normal", testDemoToken, nil)
	if w.Code != http.StatusOK || hits.Load() != 1 {
		t.Fatalf("normal action: code=%d hits=%d body=%s", w.Code, hits.Load(), w.Body.String())
	}
	if second := demoRequest(g, http.MethodPost, "/demo/actions/normal", testDemoToken, nil); second.Code != http.StatusTooManyRequests || second.Header().Get("Retry-After") == "" {
		t.Fatalf("cooldown: %d %v", second.Code, second.Header())
	}
}

func TestDemoRateBurstUsesRealLimiterAndFixedCap(t *testing.T) {
	endpoint := os.Getenv("REDIS_TEST_URL")
	if endpoint == "" {
		t.Skip("set REDIS_TEST_URL for real Redis demo limiter check")
	}
	limiter, err := ratelimit.New(ratelimit.Config{URL: endpoint, Namespace: fmt.Sprintf("demotest%d", time.Now().UnixNano()), Timeout: 100 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer limiter.Close()
	var hits atomic.Int32
	u := upstream(t, func(w http.ResponseWriter, _ *http.Request) { hits.Add(1); w.WriteHeader(http.StatusOK) })
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{Limiter: limiter, RatePolicies: [2]ratelimit.Policy{{Limit: 2, Window: time.Minute}, {Limit: 2, Window: time.Minute}}, DemoToken: testDemoToken})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	w := demoRequest(g, http.MethodPost, "/demo/actions/rate-burst", testDemoToken, nil)
	if w.Code != http.StatusOK {
		t.Fatalf("action=%d %s", w.Code, w.Body.String())
	}
	var result gateway.DemoActionResult
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.RequestCount != 3 || result.Statuses["OK"] != 2 || result.Statuses["Too Many Requests"] != 1 || hits.Load() != 2 {
		t.Fatalf("burst result=%+v upstream hits=%d", result, hits.Load())
	}
	state := demoRequest(g, http.MethodGet, "/demo/status", testDemoToken, nil)
	var snapshot gateway.DemoStatus
	if err := json.Unmarshal(state.Body.Bytes(), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Routes[0].Outcomes["429"] != 1 || snapshot.Routes[0].Limited != 1 || snapshot.Routes[0].AverageUpstreamSeconds == nil || snapshot.Routes[0].AverageRedisSeconds == nil {
		t.Fatalf("metrics did not sample the real burst: %+v", snapshot.Routes[0])
	}
}
