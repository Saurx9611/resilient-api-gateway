package gateway

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type brokenBody struct{}

func (brokenBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func (brokenBody) Close() error             { return nil }

func TestCircuitOutcomeClassification(t *testing.T) {
	for _, tc := range []struct {
		name      string
		code      int
		transport bool
		broken    bool
		cancel    bool
		deadline  bool
		upload    bool
		abandon   bool
		want      breaker.State
	}{
		{name: "success", code: 200, want: breaker.Closed},
		{name: "client 404", code: 404, want: breaker.Closed},
		{name: "client 429", code: 429, want: breaker.Closed},
		{name: "upstream 500", code: 500, want: breaker.Open},
		{name: "upstream 503", code: 503, want: breaker.Open},
		{name: "transport", transport: true, want: breaker.Open},
		{name: "truncated body", code: 200, broken: true, want: breaker.Open},
		{name: "client cancellation", transport: true, cancel: true, want: breaker.Closed},
		{name: "gateway deadline", transport: true, deadline: true, want: breaker.Open},
		{name: "upload error", transport: true, upload: true, want: breaker.Closed},
		{name: "abandoned body", code: 200, abandon: true, want: breaker.Closed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, _ := breaker.New(breaker.Policy{FailureThreshold: 1, OpenDuration: time.Second, MaxProbes: 1, RecoverySuccesses: 1}, nil)
			p, _ := b.Acquire()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if tc.cancel {
				cancel()
			}
			if tc.deadline {
				var stop context.CancelFunc
				ctx, stop = context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
				defer stop()
			}
			a := &circuitAttempt{permit: p, ctx: ctx}
			if tc.upload {
				a.uploadFailed.Store(true)
			}
			r, _ := http.NewRequestWithContext(context.WithValue(ctx, attemptKey{}, a), "GET", "http://upstream.invalid/", nil)
			transport := circuitTransport{base: roundTripFunc(func(*http.Request) (*http.Response, error) {
				if tc.transport {
					return nil, errors.New("controlled transport failure")
				}
				var body io.ReadCloser = io.NopCloser(strings.NewReader("ok"))
				if tc.broken {
					body = brokenBody{}
				}
				return &http.Response{StatusCode: tc.code, Body: body}, nil
			}), metrics: &Metrics{}, route: 0}
			res, _ := transport.RoundTrip(r)
			if res != nil {
				if !tc.abandon {
					io.Copy(io.Discard, res.Body)
				}
				res.Body.Close()
			}
			a.complete(breaker.Neutral)
			if s := b.Snapshot(); s.State != tc.want {
				t.Fatal(s)
			}
		})
	}
}
