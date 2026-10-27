// Command upstream-sim is a controlled local HTTP echo service, not a fault API.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"example.com/resilient-api-gateway/internal/server"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:9001", "HTTP listen address")
	name := flag.String("name", "one", "fixed simulator name")
	flag.Parse()
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		logger.Error("cannot bind simulator listener")
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Upstream", *name)
		_ = json.NewEncoder(w).Encode(struct {
			Upstream string `json:"upstream"`
			Method   string `json:"method"`
			Path     string `json:"path"`
			Query    string `json:"query"`
		}{*name, r.Method, r.URL.Path, r.URL.RawQuery})
	})
	logger.Info("simulator listening", "address", ln.Addr().String(), "name", *name)
	if err := server.New(handler).Serve(ctx, ln, 10*time.Second, logger); err != nil {
		logger.Error("simulator stopped")
		os.Exit(1)
	}
}
