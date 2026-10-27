package gateway

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// Metrics uses fixed route, outcome, state, and duration buckets; no request data
// becomes a label. Histograms are cumulative Prometheus buckets in seconds.
type Metrics struct {
	requests      [2][7]atomic.Uint64  // route, finite outcome
	latency       [2][10]atomic.Uint64 // nine finite bounds plus +Inf
	latencyNanos  [2]atomic.Uint64
	upstream      [2][10]atomic.Uint64
	upstreamNanos [2]atomic.Uint64
	redis         [2][10]atomic.Uint64
}

var metricBounds = [...]float64{.001, .0025, .005, .01, .025, .05, .1, .25, .5}
var statusLabels = [...]string{"2xx", "3xx", "4xx", "429", "5xx", "502", "503"}

func bucket(d time.Duration) int {
	seconds := d.Seconds()
	for i, bound := range metricBounds {
		if seconds <= bound {
			return i
		}
	}
	return len(metricBounds)
}
func (m *Metrics) observeRequest(route, status int, d time.Duration) {
	if route < 0 || route > 1 {
		return
	}
	outcome := 0
	switch {
	case status == 429:
		outcome = 3
	case status == 502:
		outcome = 5
	case status == 503:
		outcome = 6
	case status >= 500:
		outcome = 4
	case status >= 400:
		outcome = 2
	case status >= 300:
		outcome = 1
	}
	m.requests[route][outcome].Add(1)
	m.latency[route][bucket(d)].Add(1)
	m.latencyNanos[route].Add(uint64(d))
}
func observeHistogram(h *[10]atomic.Uint64, d time.Duration) { h[bucket(d)].Add(1) }

func requestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b[:])
}

// MetricsHandler serves text exposition; deployments should bind it privately.
func (g *Gateway) MetricsHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	m := &g.metrics
	fmt.Fprintln(w, "# HELP gateway_requests_total Completed gateway route requests by finite status class.")
	fmt.Fprintln(w, "# TYPE gateway_requests_total counter")
	fmt.Fprintln(w, "# HELP gateway_request_duration_seconds Handler duration including streamed response copying.")
	fmt.Fprintln(w, "# TYPE gateway_request_duration_seconds histogram")
	fmt.Fprintln(w, "# HELP gateway_upstream_roundtrip_duration_seconds Shared transport time through upstream response headers.")
	fmt.Fprintln(w, "# TYPE gateway_upstream_roundtrip_duration_seconds histogram")
	fmt.Fprintln(w, "# HELP gateway_redis_admission_duration_seconds Limiter Check wall time.")
	fmt.Fprintln(w, "# TYPE gateway_redis_admission_duration_seconds histogram")
	fmt.Fprintln(w, "# HELP gateway_redis_errors_total Redis admission dependency errors.")
	fmt.Fprintln(w, "# TYPE gateway_redis_errors_total counter")
	fmt.Fprintln(w, "# HELP gateway_admission_total Admission decisions by finite outcome.")
	fmt.Fprintln(w, "# TYPE gateway_admission_total counter")
	fmt.Fprintln(w, "# HELP gateway_circuit_state Current process-local circuit state, one-hot by route.")
	fmt.Fprintln(w, "# TYPE gateway_circuit_state gauge")
	fmt.Fprintln(w, "# HELP gateway_circuit_active_probes Current half-open probes.")
	fmt.Fprintln(w, "# TYPE gateway_circuit_active_probes gauge")
	fmt.Fprintln(w, "# HELP gateway_circuit_rejected_total Circuit admissions rejected.")
	fmt.Fprintln(w, "# TYPE gateway_circuit_rejected_total counter")
	fmt.Fprintln(w, "# HELP gateway_circuit_outcomes_total Completed upstream outcomes observed by the process-local circuit.")
	fmt.Fprintln(w, "# TYPE gateway_circuit_outcomes_total counter")
	fmt.Fprintln(w, "# HELP gateway_circuit_transitions_total Process-local circuit state transitions.")
	fmt.Fprintln(w, "# TYPE gateway_circuit_transitions_total counter")
	for route := 0; route < 2; route++ {
		id := [...]string{"one", "two"}[route]
		for outcome, name := range statusLabels {
			fmt.Fprintf(w, "gateway_requests_total{route=%q,outcome=%q} %d\n", id, name, m.requests[route][outcome].Load())
		}
		writeHistogram(w, "gateway_request_duration_seconds", id, &m.latency[route], m.latencyNanos[route].Load())
		writeHistogram(w, "gateway_upstream_roundtrip_duration_seconds", id, &m.upstream[route], m.upstreamNanos[route].Load())
		admission := g.AdmissionStats()[route]
		writeHistogram(w, "gateway_redis_admission_duration_seconds", id, &m.redis[route], admission.RedisNanos)
		fmt.Fprintf(w, "gateway_redis_errors_total{route=%q} %d\n", id, admission.RedisErrors)
		for _, item := range []struct {
			name  string
			value uint64
		}{{"admitted", admission.Admitted}, {"limited", admission.Limited}, {"fail_open", admission.FailOpen}, {"fail_closed", admission.FailClosed}, {"capacity", admission.Capacity}, {"invalid_identity", admission.InvalidIdentity}} {
			fmt.Fprintf(w, "gateway_admission_total{route=%q,outcome=%q} %d\n", id, item.name, item.value)
		}
		snap := g.CircuitStats()[route]
		fmt.Fprintf(w, "gateway_circuit_active_probes{route=%q} %d\n", id, snap.ActiveProbes)
		fmt.Fprintf(w, "gateway_circuit_rejected_total{route=%q} %d\n", id, snap.Rejected)
		for _, item := range []struct {
			name  string
			value uint64
		}{{"success", snap.Successes}, {"failure", snap.Failures}, {"neutral", snap.Neutral}} {
			fmt.Fprintf(w, "gateway_circuit_outcomes_total{route=%q,outcome=%q} %d\n", id, item.name, item.value)
		}
		for _, state := range []string{"closed", "open", "half_open"} {
			value := 0
			if string(snap.State) == state {
				value = 1
			}
			fmt.Fprintf(w, "gateway_circuit_state{route=%q,state=%q} %d\n", id, state, value)
		}
		for from := 0; from < 3; from++ {
			for to := 0; to < 3; to++ {
				fmt.Fprintf(w, "gateway_circuit_transitions_total{route=%q,from=%q,to=%q} %d\n", id, []string{"closed", "open", "half_open"}[from], []string{"closed", "open", "half_open"}[to], snap.Transitions[from][to])
			}
		}
	}
}
func writeHistogram(w http.ResponseWriter, name, route string, h *[10]atomic.Uint64, nanos uint64) {
	var cumulative uint64
	for i, bound := range metricBounds {
		cumulative += h[i].Load()
		fmt.Fprintf(w, "%s_bucket{route=%q,le=%q} %d\n", name, route, strconv.FormatFloat(bound, 'f', -1, 64), cumulative)
	}
	var count uint64
	for i := range h {
		count += h[i].Load()
	}
	fmt.Fprintf(w, "%s_bucket{route=%q,le=\"+Inf\"} %d\n%s_sum{route=%q} %.9f\n%s_count{route=%q} %d\n", name, route, count, name, route, float64(nanos)/1e9, name, route, count)
}

func validRequestID(v string) bool {
	if len(v) < 1 || len(v) > 64 {
		return false
	}
	for _, c := range v {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("._-", c)) {
			return false
		}
	}
	return true
}
