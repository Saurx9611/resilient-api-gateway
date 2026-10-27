package gateway

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"

	"example.com/resilient-api-gateway/internal/breaker"
)

// Config is copied by value. Prefixes, quota/circuit/transport policy and Redis
// identity remain startup settings; a reload only changes these operator fields.
type Config struct {
	Targets      [2]string
	RedisFailure [2]string
	Readiness    string
}
type snapshot struct {
	routes  [2]route
	config  Config
	version uint64
}
type ReloadStats struct{ Version, Applied, Rejected uint64 }

func (g *Gateway) ReloadStats() ReloadStats {
	g.reloadMu.Lock()
	defer g.reloadMu.Unlock()
	return ReloadStats{g.current.Load().version, g.applied, g.rejected}
}
func (g *Gateway) CurrentConfig() Config { return g.current.Load().config }

// Reload builds privately then publishes once. Failed builds retain the last
// good snapshot. In-flight requests retain their original route/proxy/policy.
func (g *Gateway) Reload(cfg Config) error {
	g.reloadMu.Lock()
	defer g.reloadMu.Unlock()
	return g.reload(cfg)
}
func (g *Gateway) reload(cfg Config) error {
	old := g.current.Load()
	next, err := g.buildSnapshot(cfg, old)
	if err != nil {
		g.rejected++
		return err
	}
	if next.config == old.config {
		return nil
	}
	next.version = old.version + 1
	g.current.Store(next)
	g.applied++
	return nil
}
func (g *Gateway) buildSnapshot(cfg Config, old *snapshot) (*snapshot, error) {
	if cfg.Readiness == "" {
		cfg.Readiness = "required"
	}
	if cfg.Readiness != "required" && cfg.Readiness != "policy" {
		return nil, errors.New("invalid readiness mode")
	}
	opts := g.opts
	tr := g.transport
	targets := cfg.Targets
	policies := opts.RatePolicies
	for i, mode := range cfg.RedisFailure {
		if mode != "closed" && mode != "open" {
			return nil, errors.New("invalid Redis failure mode")
		}
		policies[i].FailOpen = mode == "open"
	}
	var origins [2]*url.URL
	for i, raw := range targets {
		u, err := url.Parse(raw)
		if err != nil || u == nil || (u.Scheme != "http" && u.Scheme != "https") ||
			u.Hostname() == "" || u.User != nil || u.Opaque != "" ||
			(u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.ForceQuery ||
			u.Fragment != "" || strings.ContainsAny(raw, "\\#") {
			return nil, fmt.Errorf("upstream %d must be an HTTP(S) origin without credentials, path, query or fragment", i+1)
		}
		origins[i] = u
		if port := u.Port(); port != "" {
			n, err := strconv.Atoi(port)
			if err != nil || n < 1 || n > 65535 {
				return nil, fmt.Errorf("upstream %d has an invalid port", i+1)
			}
		} else if strings.HasSuffix(u.Host, ":") {
			return nil, fmt.Errorf("upstream %d has an empty port", i+1)
		}
	}

	s := &snapshot{config: cfg}
	circuits := make(map[string]*breaker.Breaker)
	if old != nil {
		for _, r := range old.routes {
			circuits[r.origin] = r.circuit
		}
	}
	for i, prefix := range [2]string{"/one", "/two"} {
		target := origins[i]
		port := target.Port()
		if port == "" {
			port = "80"
			if target.Scheme == "https" {
				port = "443"
			}
		} else {
			n, _ := strconv.Atoi(port)
			port = strconv.Itoa(n)
		}
		origin := target.Scheme + "://" + net.JoinHostPort(strings.ToLower(target.Hostname()), port)
		circuit := circuits[origin]
		if circuit == nil {
			circuit, _ = breaker.New(opts.CircuitPolicy, opts.CircuitNow)
			circuits[origin] = circuit
		}
		s.routes[i] = route{prefix: prefix, policy: policies[i], circuit: circuit, origin: origin, proxy: &httputil.ReverseProxy{
			Transport: circuitTransport{base: tr, metrics: &g.metrics, route: i},
			Rewrite: func(pr *httputil.ProxyRequest) {
				pr.SetURL(target)
				pr.Out.URL.Path = strings.TrimPrefix(pr.In.URL.Path, prefix)
				pr.Out.URL.RawPath = strings.TrimPrefix(pr.In.URL.EscapedPath(), prefix)
				if pr.Out.URL.Path == "" {
					pr.Out.URL.Path, pr.Out.URL.RawPath = "/", ""
				}
				pr.Out.URL.RawQuery = pr.In.URL.RawQuery // validated before proxying
				for key := range pr.Out.Header {
					lower := strings.ToLower(key)
					if lower == "forwarded" || lower == "x-real-ip" || strings.HasPrefix(lower, "x-forwarded-") {
						delete(pr.Out.Header, key)
					}
				}
				pr.SetXForwarded() // immediate peer only; never append client claims
			},
			FlushInterval: -1,
			ModifyResponse: func(res *http.Response) error {
				if opts.Limiter == nil {
					return nil
				}
				for key := range res.Trailer {
					if quotaField(key) {
						return errors.New("reserved quota trailer")
					}
				}
				for key := range res.Header {
					if quotaField(key) {
						res.Header.Del(key)
					}
				}
				res.Header.Del("Cache-Control") // gateway's per-identity response is no-store
				return nil
			},
			ErrorLog: log.New(redactedLog{opts.Logger}, "", 0),
			ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
				if errors.Is(r.Context().Err(), context.Canceled) {
					return
				}
				status := http.StatusBadGateway
				var timeout net.Error
				if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &timeout) && timeout.Timeout()) {
					status = http.StatusGatewayTimeout
				}
				opts.Logger.Warn("upstream request failed", "route", prefix, "status", status)
				reject(w, status)
			},
		}}
	}
	return s, nil
}
