package gateway

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"example.com/resilient-api-gateway/internal/ratelimit"
)

// Admission is the Redis integration/test boundary. Implementations must honor ctx.
type Admission interface {
	Check(context.Context, string, string, ratelimit.Policy) (ratelimit.Decision, error)
}

type AdmissionEvent struct {
	Route   string
	Outcome string
	Elapsed time.Duration
}

type AdmissionStats struct {
	Admitted, Limited, FailOpen, FailClosed, Capacity, InvalidIdentity uint64
	RedisErrors, RedisNanos                                            uint64
}

type admissionCounters struct {
	outcomes                [6]atomic.Uint64
	redisErrors, redisNanos atomic.Uint64
}

// AdmissionStats returns fixed per-route counters: index 0=one, index 1=two.
// There are no identity, URL, path, or error-string labels.
func (g *Gateway) AdmissionStats() [2]AdmissionStats {
	var result [2]AdmissionStats
	for i := range g.stats {
		s := &g.stats[i]
		result[i] = AdmissionStats{s.outcomes[0].Load(), s.outcomes[1].Load(), s.outcomes[2].Load(), s.outcomes[3].Load(), s.outcomes[4].Load(), s.outcomes[5].Load(), s.redisErrors.Load(), s.redisNanos.Load()}
	}
	return result
}

var outcomeNames = [6]string{"admitted", "limited", "fail_open", "fail_closed", "capacity", "invalid_identity"}

func (g *Gateway) observe(index, outcome int, elapsed time.Duration, dependencyError bool) {
	observeHistogram(&g.metrics.redis[index], elapsed)
	g.stats[index].outcomes[outcome].Add(1)
	if dependencyError {
		g.stats[index].redisErrors.Add(1)
	}
	g.stats[index].redisNanos.Add(uint64(elapsed))
	if g.observer != nil {
		g.observer(AdmissionEvent{Route: [2]string{"one", "two"}[index], Outcome: outcomeNames[outcome], Elapsed: elapsed})
	}
}

func seconds(d time.Duration) string {
	n := (d + time.Second - 1) / time.Second
	if n < 1 {
		n = 1
	}
	return strconv.FormatInt(int64(n), 10)
}

func quotaField(name string) bool {
	return strings.EqualFold(name, "RateLimit-Limit") || strings.EqualFold(name, "RateLimit-Remaining") || strings.EqualFold(name, "RateLimit-Reset")
}

func (g *Gateway) admit(w http.ResponseWriter, r *http.Request, index int, rt route) bool {
	if g.admission == nil {
		return true
	} // explicit library mode; CLI always enables it
	identity, err := ratelimit.PeerID(r.RemoteAddr)
	if err != nil {
		g.observe(index, 5, 0, false)
		reject(w, http.StatusBadRequest)
		return false
	}
	w.Header().Set("Cache-Control", "no-store")
	started := time.Now()
	decision, err := g.admission.Check(r.Context(), strings.TrimPrefix(rt.prefix, "/"), identity, rt.policy)
	elapsed := time.Since(started)
	if err := r.Context().Err(); err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			reject(w, http.StatusGatewayTimeout)
		}
		return false
	}
	if err != nil {
		outcome := 3
		if rt.policy.FailOpen {
			outcome = 2
		}
		g.observe(index, outcome, elapsed, true)
		g.logger.Warn("rate limiter dependency failure", "route", strings.TrimPrefix(rt.prefix, "/"), "policy", outcomeNames[outcome])
		if rt.policy.FailOpen {
			return true
		}
		w.Header().Set("Retry-After", "1")
		reject(w, http.StatusServiceUnavailable)
		return false
	}
	if decision.Capacity {
		g.observe(index, 4, elapsed, false)
		w.Header().Set("Retry-After", seconds(decision.RetryAfter))
		reject(w, http.StatusServiceUnavailable)
		return false
	}
	w.Header().Set("RateLimit-Limit", strconv.FormatInt(rt.policy.Limit, 10))
	w.Header().Set("RateLimit-Remaining", strconv.FormatInt(decision.Remaining, 10))
	w.Header().Set("RateLimit-Reset", seconds(decision.Reset))
	if !decision.Allowed {
		g.observe(index, 1, elapsed, false)
		w.Header().Set("Retry-After", seconds(decision.RetryAfter))
		reject(w, http.StatusTooManyRequests)
		return false
	}
	g.observe(index, 0, elapsed, false)
	return true
}
