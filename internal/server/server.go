// Package server owns HTTP health, readiness and bounded graceful shutdown.
package server

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

type Server struct {
	handler      http.Handler
	metricsToken string
	ready        atomic.Bool
}

func New(handler http.Handler) *Server { return &Server{handler: handler} }

// NewWithMetricsToken protects only /metrics. Health probes remain suitable
// for platform checks; deployments should also keep the listener private.
func NewWithMetricsToken(handler http.Handler, token string) *Server {
	return &Server{handler: handler, metricsToken: token}
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.IsAbs() || r.URL.Host != "" || !strings.HasPrefix(r.RequestURI, "/") {
		w.Header().Set("Connection", "close")
		http.Error(w, "Bad Request", http.StatusBadRequest)
		return
	}
	path := r.URL.EscapedPath()
	if path == "/metrics" {
		if s.metricsToken != "" {
			provided := ""
			parts := strings.Fields(r.Header.Get("Authorization"))
			if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
				provided = parts[1]
			}
			if len(provided) != len(s.metricsToken) || subtle.ConstantTimeCompare([]byte(provided), []byte(s.metricsToken)) != 1 {
				w.Header().Set("WWW-Authenticate", `Bearer realm="metrics"`)
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
			return
		}
		if endpoint, ok := s.handler.(interface {
			MetricsHandler(http.ResponseWriter, *http.Request)
		}); ok {
			endpoint.MetricsHandler(w, r)
			return
		}
		http.NotFound(w, r)
		return
	}
	if path == "/healthz" || path == "/readyz" {
		if r.ContentLength != 0 {
			w.Header().Set("Connection", "close")
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			w.Header().Set("Connection", "close")
			http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		if path == "/readyz" && !s.ready.Load() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		if path == "/readyz" {
			if dependency, ok := s.handler.(interface{ Ready(context.Context) error }); ok {
				if err := dependency.Ready(r.Context()); err != nil {
					http.Error(w, "not ready", http.StatusServiceUnavailable)
					return
				}
			}
			// A readiness probe may have overlapped the start of draining.
			if !s.ready.Load() {
				http.Error(w, "not ready", http.StatusServiceUnavailable)
				return
			}
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprintln(w, "ok")
		return
	}
	if !s.ready.Load() {
		w.Header().Set("Connection", "close")
		http.Error(w, "not ready", http.StatusServiceUnavailable)
		return
	}
	s.handler.ServeHTTP(w, r)
}

// Serve owns ln and is called once. Signal ctx only initiates draining; the
// independent base context remains live until draining finishes or times out.
func (s *Server) Serve(ctx context.Context, ln net.Listener, shutdownTimeout time.Duration, logger *slog.Logger) error {
	defer ln.Close()
	if shutdownTimeout <= 0 {
		return errors.New("shutdown timeout must be positive")
	}
	if logger == nil {
		logger = slog.Default()
	}
	base, cancel := context.WithCancel(context.Background())
	defer cancel()
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	srv := &http.Server{
		Handler:           s,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      35 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    32 << 10,
		Protocols:         protocols,
		BaseContext:       func(net.Listener) context.Context { return base },
	}
	served := make(chan error, 1)
	s.ready.Store(true)
	defer s.ready.Store(false)
	go func() { served <- srv.Serve(ln) }()
	select {
	case err := <-served:
		s.ready.Store(false)
		cancel()
		_ = srv.Close()
		return err
	case <-ctx.Done():
	}
	s.ready.Store(false)
	logger.Info("shutdown started")
	drain, stop := context.WithTimeout(context.Background(), shutdownTimeout)
	defer stop()
	err := srv.Shutdown(drain)
	if err != nil {
		cancel()
		_ = srv.Close()
	}
	serveErr := <-served
	if !errors.Is(serveErr, http.ErrServerClosed) {
		return errors.Join(err, serveErr)
	}
	return err
}
