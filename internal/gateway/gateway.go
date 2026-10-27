// Package gateway builds a private, immutable pair of HTTP routes.
package gateway

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
	"example.com/resilient-api-gateway/internal/ratelimit"
)

// Options exposes policies with independent integration tests.
// Zero values select conservative defaults. Negative values are invalid.
type Options struct {
	RequestTimeout        time.Duration
	ResponseHeaderTimeout time.Duration
	MaxConnsPerHost       int
	Logger                *slog.Logger
	Limiter               Admission
	// RedisHealth must honor ctx; nil is unhealthy when an Admission is present.
	RedisHealth  func(context.Context) error
	RatePolicies [2]ratelimit.Policy
	// ObserveAdmission must be concurrency-safe and nonblocking. Never receives identities.
	ObserveAdmission func(AdmissionEvent)
	CircuitPolicy    breaker.Policy
	// CircuitNow must be concurrency-safe. Nil selects time.Now.
	CircuitNow func() time.Time
	// DemoToken enables fixed, bounded demo status/actions; use a server-side secret of at least 32 bytes.
	DemoToken string
}

type route struct {
	prefix  string
	proxy   *httputil.ReverseProxy
	policy  ratelimit.Policy
	circuit *breaker.Breaker
	origin  string
}

// Gateway owns one reusable transport and atomically replaced route snapshots.
type Gateway struct {
	current           atomic.Pointer[snapshot]
	reloadMu          sync.Mutex
	applied, rejected uint64
	opts              Options
	transport         *http.Transport
	timeout           time.Duration
	admission         Admission
	logger            *slog.Logger
	observer          func(AdmissionEvent)
	stats             [2]admissionCounters
	metrics           Metrics
	demo              *demoController
}

// New treats targets as privileged operator allowlist entries, never client input.
// The first target serves /one and the second /two; each prefix is stripped.
func New(targets [2]string, opts Options) (*Gateway, error) {
	if len(opts.DemoToken) != 0 && (len(opts.DemoToken) < 32 || len(opts.DemoToken) > 256) {
		return nil, errors.New("demo token must contain 32..256 bytes")
	}
	if opts.RequestTimeout < 0 || opts.ResponseHeaderTimeout < 0 || opts.MaxConnsPerHost < 0 {
		return nil, errors.New("gateway limits must not be negative")
	}
	if opts.RequestTimeout == 0 {
		opts.RequestTimeout = 30 * time.Second
	}
	if opts.ResponseHeaderTimeout == 0 {
		opts.ResponseHeaderTimeout = 5 * time.Second
	}
	if opts.MaxConnsPerHost == 0 {
		opts.MaxConnsPerHost = 64
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.CircuitPolicy == (breaker.Policy{}) {
		opts.CircuitPolicy = breaker.DefaultPolicy()
	}
	if err := opts.CircuitPolicy.Validate(); err != nil {
		return nil, err
	}
	if opts.Limiter != nil {
		for i, policy := range opts.RatePolicies {
			if policy == (ratelimit.Policy{}) {
				opts.RatePolicies[i] = ratelimit.DefaultPolicy()
			}
			if err := opts.RatePolicies[i].Validate(); err != nil {
				return nil, err
			}
		}
	}
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	tr := &http.Transport{
		Proxy:                  nil,
		DialContext:            (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:    3 * time.Second,
		ResponseHeaderTimeout:  opts.ResponseHeaderTimeout,
		ExpectContinueTimeout:  time.Second,
		IdleConnTimeout:        90 * time.Second,
		MaxIdleConns:           128,
		MaxIdleConnsPerHost:    16,
		MaxConnsPerHost:        opts.MaxConnsPerHost,
		MaxResponseHeaderBytes: 32 << 10,
		DisableCompression:     true,
		Protocols:              protocols,
	}
	g := &Gateway{transport: tr, timeout: opts.RequestTimeout, admission: opts.Limiter, logger: opts.Logger, observer: opts.ObserveAdmission, opts: opts}
	if opts.DemoToken != "" {
		g.demo = &demoController{token: opts.DemoToken, next: make(map[string]time.Time)}
	}
	cfg := Config{Targets: targets, Readiness: "required"}
	for i, p := range opts.RatePolicies {
		cfg.RedisFailure[i] = "closed"
		if p.FailOpen {
			cfg.RedisFailure[i] = "open"
		}
	}
	initial, err := g.buildSnapshot(cfg, nil)
	if err != nil {
		return nil, err
	}
	initial.version = 1
	g.current.Store(initial)

	return g, nil
}

// CloseIdleConnections releases pooled sockets after the HTTP server has drained.
func (g *Gateway) CloseIdleConnections() { g.transport.CloseIdleConnections() }

const allowedMethods = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w *statusWriter) WriteHeader(code int) {
	if code >= 200 && w.status == 0 {
		w.status = code
	}
	w.ResponseWriter.WriteHeader(code)
}
func (w *statusWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(p)
}
func (w *statusWriter) Flush() {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	_ = http.NewResponseController(w.ResponseWriter).Flush()
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	id := r.Header.Get("X-Request-ID")
	if !validRequestID(id) {
		id = requestID()
	}
	w.Header().Set("X-Request-ID", id)
	started := time.Now()
	routeIndex := -1
	if strings.HasPrefix(r.URL.Path, "/one/") || r.URL.Path == "/one" {
		routeIndex = 0
	}
	if strings.HasPrefix(r.URL.Path, "/two/") || r.URL.Path == "/two" {
		routeIndex = 1
	}
	sw := &statusWriter{ResponseWriter: w}
	defer func() {
		status := sw.status
		if status == 0 {
			status = http.StatusOK
		}
		elapsed := time.Since(started)
		if routeIndex >= 0 {
			g.metrics.observeRequest(routeIndex, status, elapsed)
		}
		routeName := "unmatched"
		if routeIndex >= 0 {
			routeName = [...]string{"one", "two"}[routeIndex]
		}
		g.logger.Info("request complete", "request_id", id, "route", routeName, "status", status, "duration_seconds", elapsed.Seconds())
	}()
	g.serveHTTP(sw, r)
}

func (g *Gateway) serveHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.IsAbs() || r.URL.Host != "" || !strings.HasPrefix(r.RequestURI, "/") || !validPath(r.URL) {
		reject(w, http.StatusBadRequest)
		return
	}
	if _, err := url.ParseQuery(r.URL.RawQuery); err != nil {
		reject(w, http.StatusBadRequest)
		return
	}
	if g.serveDemo(w, r) {
		return
	}
	selected := g.current.Load()
	for index, rt := range selected.routes {
		if r.URL.Path != rt.prefix && !strings.HasPrefix(r.URL.Path, rt.prefix+"/") {
			continue
		}
		escaped := r.URL.EscapedPath()
		if escaped != rt.prefix && !strings.HasPrefix(escaped, rt.prefix+"/") {
			reject(w, http.StatusBadRequest)
			return
		}
		switch r.Method {
		case "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS":
		default:
			w.Header().Set("Allow", allowedMethods)
			reject(w, http.StatusMethodNotAllowed)
			return
		}
		if r.Header.Get("Upgrade") != "" || hasUpgradeToken(r.Header.Values("Connection")) {
			reject(w, http.StatusNotImplemented)
			return
		}
		if len(r.Trailer) != 0 {
			// ReverseProxy clones the trailer map before upload EOF populates it.
			// Reject explicitly instead of silently losing trailer values.
			reject(w, http.StatusNotImplemented)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), g.timeout)
		defer cancel()
		r = r.WithContext(ctx)
		if !g.admit(w, r, index, rt) {
			return
		}
		if r.Context().Err() != nil {
			return
		}
		permit, retry := rt.circuit.Acquire()
		if permit == nil {
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Retry-After", seconds(retry))
			reject(w, http.StatusServiceUnavailable)
			return
		}
		attempt := &circuitAttempt{permit: permit, ctx: r.Context()}
		defer attempt.complete(breaker.Neutral)
		r = r.WithContext(context.WithValue(r.Context(), attemptKey{}, attempt))
		if r.Body != nil && r.Body != http.NoBody {
			r.Body = uploadBody{r.Body, attempt}
		}
		// Supported by the native HTTP/1 server; best effort for wrapped writers.
		_ = http.NewResponseController(w).EnableFullDuplex()
		rt.proxy.ServeHTTP(flushResponseHeaders{w}, r)
		return
	}
	reject(w, http.StatusNotFound)
}

func validPath(u *url.URL) bool {
	if !strings.HasPrefix(u.Path, "/") || strings.Contains(u.Path, "\\") || strings.Contains(u.Path, "//") {
		return false
	}
	for _, c := range u.Path {
		if c < 32 || c == 127 {
			return false
		}
	}
	for _, part := range strings.Split(u.Path, "/") {
		if part == "." || part == ".." {
			return false
		}
	}
	escaped := strings.ToLower(u.EscapedPath())
	return !strings.Contains(escaped, "%2f") && !strings.Contains(escaped, "%5c")
}

func hasUpgradeToken(values []string) bool {
	for _, value := range values {
		for _, token := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return true
			}
		}
	}
	return false
}

func reject(w http.ResponseWriter, status int) {
	// Avoid net/http draining an untrusted/stalled upload before rejection.
	// Already-buffered bytes are outside handler control.
	w.Header().Set("Connection", "close")
	http.Error(w, http.StatusText(status), status)
}

// Keep stream error events without upstream-supplied diagnostic data.
type redactedLog struct{ logger *slog.Logger }

func (l redactedLog) Write(p []byte) (int, error) {
	l.logger.Error("proxy stream or protocol error")
	return len(p), nil
}

// A zero-length early response must reach an uploading client before deferred
// request-body Close can block. ReverseProxy's negative flush timer alone races
// with completion of an empty copy. Do not flush interim 1xx into a final 200.
type flushResponseHeaders struct{ http.ResponseWriter }

func (w flushResponseHeaders) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w flushResponseHeaders) WriteHeader(status int) {
	w.ResponseWriter.WriteHeader(status)
	if status >= 200 {
		_ = http.NewResponseController(w.ResponseWriter).Flush()
	}
}
