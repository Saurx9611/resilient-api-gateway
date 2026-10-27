package gateway

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
)

const maxDemoBurstRequests int64 = 64

type demoController struct {
	token       string
	mu          sync.Mutex
	active      bool
	next        map[string]time.Time
	lastCircuit *DemoCircuitResult
}

type DemoStatus struct {
	SampledAt           time.Time          `json:"sampled_at"`
	Ready               bool               `json:"ready"`
	Routes              [2]DemoRoute       `json:"routes"`
	RateBurstEnabled    bool               `json:"rate_burst_enabled"`
	RateBurstRequestCap int64              `json:"rate_burst_request_cap"`
	CircuitDemo         *DemoCircuitResult `json:"isolated_circuit_demo,omitempty"`
}

type DemoRoute struct {
	ID                          string            `json:"id"`
	RequestCount                uint64            `json:"request_count"`
	Outcomes                    map[string]uint64 `json:"outcomes"`
	AverageDurationSeconds      *float64          `json:"average_duration_seconds,omitempty"`
	P95BucketUpperBoundSeconds  *float64          `json:"p95_bucket_upper_bound_seconds,omitempty"`
	P95Overflow                 bool              `json:"p95_overflow"`
	AverageUpstreamSeconds      *float64          `json:"average_upstream_seconds,omitempty"`
	P95UpstreamBucketUpperBound *float64          `json:"p95_upstream_bucket_upper_bound_seconds,omitempty"`
	UpstreamP95Overflow         bool              `json:"upstream_p95_overflow"`
	AverageRedisSeconds         *float64          `json:"average_redis_seconds,omitempty"`
	Limited                     uint64            `json:"limited"`
	RedisErrors                 uint64            `json:"redis_errors"`
	CircuitState                string            `json:"circuit_state"`
	CircuitRejected             uint64            `json:"circuit_rejected"`
	CircuitFailures             uint64            `json:"circuit_failures"`
	CircuitTransitions          [3][3]uint64      `json:"circuit_transitions"`
}

type DemoActionResult struct {
	Action               string             `json:"action"`
	SampledAt            time.Time          `json:"sampled_at"`
	RequestCount         int                `json:"request_count,omitempty"`
	Statuses             map[string]int     `json:"statuses,omitempty"`
	DurationMilliseconds float64            `json:"duration_milliseconds,omitempty"`
	Message              string             `json:"message,omitempty"`
	Circuit              *DemoCircuitResult `json:"circuit,omitempty"`
}

type DemoCircuitStep struct {
	Name                    string `json:"name"`
	SimulatedUpstreamStatus int    `json:"simulated_upstream_status,omitempty"`
	GatewayStatus           int    `json:"gateway_status"`
	State                   string `json:"state"`
}

type DemoCircuitResult struct {
	Isolated    bool              `json:"isolated"`
	SampledAt   time.Time         `json:"sampled_at"`
	Steps       []DemoCircuitStep `json:"steps"`
	Transitions [3][3]uint64      `json:"transitions"`
}

func (g *Gateway) serveDemo(w http.ResponseWriter, r *http.Request) bool {
	if r.URL.Path != "/demo/status" && !strings.HasPrefix(r.URL.Path, "/demo/actions/") {
		return false
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if g.demo == nil {
		http.NotFound(w, r)
		return true
	}
	if !g.authorizeDemo(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return true
	}
	if r.URL.RawQuery != "" {
		http.Error(w, "query not supported", http.StatusBadRequest)
		return true
	}
	if r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
		w.Header().Set("Connection", "close")
		http.Error(w, "request body not supported", http.StatusRequestEntityTooLarge)
		return true
	}
	if r.URL.Path == "/demo/status" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return true
		}
		writeDemoJSON(w, http.StatusOK, g.demoStatus(r.Context()))
		return true
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return true
	}
	action := strings.TrimPrefix(r.URL.Path, "/demo/actions/")
	if strings.Contains(action, "/") || (action != "normal" && action != "rate-burst" && action != "circuit-recovery") {
		http.NotFound(w, r)
		return true
	}
	if action == "rate-burst" && !g.rateBurstEnabled() {
		writeDemoJSON(w, http.StatusConflict, DemoActionResult{Action: action, SampledAt: time.Now().UTC(), Message: "rate burst disabled: admission is unavailable or its route limit exceeds the fixed 64 request cap"})
		return true
	}
	retry := g.beginDemoAction(action)
	if retry > 0 {
		w.Header().Set("Retry-After", seconds(retry))
		writeDemoJSON(w, http.StatusTooManyRequests, DemoActionResult{Action: action, SampledAt: time.Now().UTC(), Message: "demo controls are cooling down"})
		return true
	}
	defer g.endDemoAction()
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	var result DemoActionResult
	var err error
	switch action {
	case "normal":
		result, err = g.demoNormal(ctx)
	case "rate-burst":
		result, err = g.demoRateBurst(ctx)
	case "circuit-recovery":
		result = g.demoCircuitRecovery()
	}
	if err != nil {
		writeDemoJSON(w, http.StatusBadGateway, DemoActionResult{Action: action, SampledAt: time.Now().UTC(), Message: "demo request failed"})
		return true
	}
	writeDemoJSON(w, http.StatusOK, result)
	return true
}

func (g *Gateway) authorizeDemo(r *http.Request) bool {
	value := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if value == r.Header.Get("Authorization") || len(value) != len(g.demo.token) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(value), []byte(g.demo.token)) == 1
}

func (g *Gateway) beginDemoAction(action string) time.Duration {
	g.demo.mu.Lock()
	defer g.demo.mu.Unlock()
	now := time.Now()
	if g.demo.active {
		return time.Second
	}
	if until := g.demo.next[action]; until.After(now) {
		return until.Sub(now)
	}
	g.demo.active = true
	cooldown := 3 * time.Second
	if action == "rate-burst" || action == "circuit-recovery" {
		cooldown = time.Minute
	}
	g.demo.next[action] = now.Add(cooldown)
	return 0
}

func (g *Gateway) endDemoAction() {
	g.demo.mu.Lock()
	g.demo.active = false
	g.demo.mu.Unlock()
}

func (g *Gateway) rateBurstEnabled() bool {
	if g.admission == nil {
		return false
	}
	limit := g.current.Load().routes[0].policy.Limit
	return limit >= 1 && limit < maxDemoBurstRequests
}

func (g *Gateway) demoStatus(ctx context.Context) DemoStatus {
	ready := g.Ready(ctx) == nil
	stats := g.AdmissionStats()
	circuits := g.CircuitStats()
	result := DemoStatus{Ready: ready, RateBurstEnabled: g.rateBurstEnabled(), RateBurstRequestCap: maxDemoBurstRequests}
	for route := range result.Routes {
		metrics := &g.metrics
		outcomes := make(map[string]uint64, len(statusLabels))
		var count uint64
		for i, name := range statusLabels {
			value := metrics.requests[route][i].Load()
			outcomes[name] = value
			count += value
		}
		var avg, p95, upstreamAvg, upstreamP95, redisAvg *float64
		if count > 0 {
			mean := float64(metrics.latencyNanos[route].Load()) / float64(count) / float64(time.Second)
			avg = &mean
			upper, bounded := histogramUpperBound(&metrics.latency[route], count, .95)
			if bounded {
				p95 = &upper
			}
		}
		var upstreamCount uint64
		for i := range metrics.upstream[route] {
			upstreamCount += metrics.upstream[route][i].Load()
		}
		if upstreamCount > 0 {
			mean := float64(metrics.upstreamNanos[route].Load()) / float64(upstreamCount) / float64(time.Second)
			upstreamAvg = &mean
			upper, bounded := histogramUpperBound(&metrics.upstream[route], upstreamCount, .95)
			if bounded {
				upstreamP95 = &upper
			}
		}
		admissionCount := stats[route].Admitted + stats[route].Limited + stats[route].FailOpen + stats[route].FailClosed + stats[route].Capacity
		if admissionCount > 0 {
			mean := float64(stats[route].RedisNanos) / float64(admissionCount) / float64(time.Second)
			redisAvg = &mean
		}
		s := circuits[route]
		result.Routes[route] = DemoRoute{ID: [...]string{"one", "two"}[route], RequestCount: count, Outcomes: outcomes, AverageDurationSeconds: avg, P95BucketUpperBoundSeconds: p95, P95Overflow: count > 0 && p95 == nil, AverageUpstreamSeconds: upstreamAvg, P95UpstreamBucketUpperBound: upstreamP95, UpstreamP95Overflow: upstreamCount > 0 && upstreamP95 == nil, AverageRedisSeconds: redisAvg, Limited: stats[route].Limited, RedisErrors: stats[route].RedisErrors, CircuitState: string(s.State), CircuitRejected: s.Rejected, CircuitFailures: s.Failures, CircuitTransitions: s.Transitions}
	}
	g.demo.mu.Lock()
	result.CircuitDemo = g.demo.lastCircuit
	g.demo.mu.Unlock()
	result.SampledAt = time.Now().UTC()
	return result
}

func histogramUpperBound(h *[10]atomic.Uint64, count uint64, quantile float64) (float64, bool) {
	target := uint64(math.Ceil(float64(count) * quantile))
	var seen uint64
	for i := 0; i < len(metricBounds); i++ {
		seen += h[i].Load()
		if seen >= target {
			return metricBounds[i], true
		}
	}
	return 0, false
}

func (g *Gateway) demoNormal(ctx context.Context) (DemoActionResult, error) {
	start := time.Now()
	status, err := g.syntheticGET(ctx, "/one/__demo/normal")
	return DemoActionResult{Action: "normal", SampledAt: time.Now().UTC(), RequestCount: 1, Statuses: map[string]int{http.StatusText(status): 1}, DurationMilliseconds: float64(time.Since(start)) / float64(time.Millisecond)}, err
}

func (g *Gateway) demoRateBurst(ctx context.Context) (DemoActionResult, error) {
	count := int(g.current.Load().routes[0].policy.Limit + 1)
	start := time.Now()
	statuses := make(map[string]int, 3)
	for i := 0; i < count; i++ {
		status, err := g.syntheticGET(ctx, "/one/__demo/rate-burst")
		if err != nil {
			return DemoActionResult{Action: "rate-burst", SampledAt: time.Now().UTC()}, err
		}
		statuses[http.StatusText(status)]++
	}
	return DemoActionResult{Action: "rate-burst", SampledAt: time.Now().UTC(), RequestCount: count, Statuses: statuses, DurationMilliseconds: float64(time.Since(start)) / float64(time.Millisecond), Message: "requests used a reserved synthetic demo identity"}, nil
}

func (g *Gateway) syntheticGET(ctx context.Context, path string) (int, error) {
	r := httptest.NewRequest(http.MethodGet, path, nil).WithContext(ctx)
	r.RemoteAddr = "192.0.2.77:41000" // reserved documentation address; isolated from visitor IP keys
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	res := w.Result()
	defer res.Body.Close()
	_, err := io.Copy(io.Discard, io.LimitReader(res.Body, 4096))
	return res.StatusCode, err
}

func (g *Gateway) demoCircuitRecovery() DemoActionResult {
	now := time.Now()
	clock := func() time.Time { return now }
	b, _ := breaker.New(breaker.Policy{FailureThreshold: 2, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 2}, clock)
	steps := make([]DemoCircuitStep, 0, 5)
	for i := 0; i < 2; i++ {
		permit, _ := b.Acquire()
		permit.Complete(breaker.Failure)
		steps = append(steps, DemoCircuitStep{Name: "isolated upstream failure", SimulatedUpstreamStatus: http.StatusInternalServerError, GatewayStatus: http.StatusInternalServerError, State: string(b.Snapshot().State)})
	}
	if permit, _ := b.Acquire(); permit == nil {
		steps = append(steps, DemoCircuitStep{Name: "circuit open fail-fast", GatewayStatus: http.StatusServiceUnavailable, State: string(b.Snapshot().State)})
	}
	now = now.Add(time.Second)
	for i := 0; i < 2; i++ {
		permit, _ := b.Acquire()
		if permit == nil {
			break
		}
		permit.Complete(breaker.Success)
		steps = append(steps, DemoCircuitStep{Name: "isolated recovery probe", SimulatedUpstreamStatus: http.StatusOK, GatewayStatus: http.StatusOK, State: string(b.Snapshot().State)})
	}
	snap := b.Snapshot()
	result := &DemoCircuitResult{Isolated: true, SampledAt: time.Now().UTC(), Steps: steps, Transitions: snap.Transitions}
	g.demo.mu.Lock()
	g.demo.lastCircuit = result
	g.demo.mu.Unlock()
	return DemoActionResult{Action: "circuit-recovery", SampledAt: result.SampledAt, Circuit: result, Message: "isolated breaker simulation; configured upstreams were not contacted"}
}

func writeDemoJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
