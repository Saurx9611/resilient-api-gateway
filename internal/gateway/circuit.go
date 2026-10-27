package gateway

import (
	"context"
	"errors"
	"io"
	"net/http"
	"sync/atomic"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
)

// CircuitStats is route-indexed; routes to the same canonical origin share state.
func (g *Gateway) CircuitStats() [2]breaker.Snapshot {
	selected := g.current.Load()
	return [2]breaker.Snapshot{selected.routes[0].circuit.Snapshot(), selected.routes[1].circuit.Snapshot()}
}

type attemptKey struct{}
type circuitAttempt struct {
	permit       *breaker.Permit
	ctx          context.Context
	uploadFailed atomic.Bool
}

func (a *circuitAttempt) complete(outcome breaker.Outcome) {
	if errors.Is(a.ctx.Err(), context.Canceled) || a.uploadFailed.Load() {
		outcome = breaker.Neutral
	}
	a.permit.Complete(outcome)
}

type uploadBody struct {
	io.ReadCloser
	a *circuitAttempt
}

func (b uploadBody) Read(p []byte) (int, error) {
	n, e := b.ReadCloser.Read(p)
	if e != nil && e != io.EOF {
		b.a.uploadFailed.Store(true)
	}
	return n, e
}

type circuitTransport struct {
	base    http.RoundTripper
	metrics *Metrics
	route   int
}

func (t circuitTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	a := r.Context().Value(attemptKey{}).(*circuitAttempt)
	started := time.Now()
	res, err := t.base.RoundTrip(r)
	elapsed := time.Since(started)
	observeHistogram(&t.metrics.upstream[t.route], elapsed)
	t.metrics.upstreamNanos[t.route].Add(uint64(elapsed))
	if err != nil {
		a.complete(breaker.Failure)
		return res, err
	}
	outcome := breaker.Success
	if res.StatusCode >= 500 && res.StatusCode <= 599 {
		outcome = breaker.Failure
	}
	if res.StatusCode >= 400 && res.StatusCode <= 499 {
		outcome = breaker.Neutral
	}
	// Empty responses have no stream to hold a probe slot. Other responses keep
	// the slot until EOF/Close, including 4xx/5xx bodies that may stream slowly.
	if res.Body == http.NoBody {
		a.complete(outcome)
	}
	res.Body = &circuitBody{ReadCloser: res.Body, a: a, outcome: outcome}
	return res, nil
}

type circuitBody struct {
	io.ReadCloser
	a       *circuitAttempt
	outcome breaker.Outcome
}

func (b *circuitBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if err == io.EOF {
		b.a.complete(b.outcome)
	} else if err != nil {
		outcome := breaker.Failure
		if b.outcome == breaker.Neutral {
			outcome = breaker.Neutral
		}
		b.a.complete(outcome)
	}
	return n, err
}
func (b *circuitBody) Close() error {
	outcome := breaker.Neutral
	if b.outcome == breaker.Failure {
		outcome = breaker.Failure
	}
	b.a.complete(outcome)
	return b.ReadCloser.Close()
}
