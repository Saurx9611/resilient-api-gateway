package main

import (
	"context"
	"crypto/sha256"
	"log/slog"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
)

// watchConfig has no HTTP surface. It runs once per process and stops with ctx.
// Invalid unchanged bytes and consecutive read errors are reported only once.
func watchConfig(ctx context.Context, path string, interval time.Duration, g *gateway.Gateway, initial []byte, logger *slog.Logger) {
	last := sha256.Sum256(initial)
	readFailed := false
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		data, err := gateway.ReadConfigFile(path)
		if err != nil {
			if !readFailed {
				g.RecordReloadFailure()
				logger.Warn("configuration reload rejected")
			}
			readFailed = true
			continue
		}
		hash := sha256.Sum256(data)
		if !readFailed && hash == last {
			continue
		}
		readFailed = false
		last = hash
		before := g.ReloadStats().Version
		if g.ReloadJSON(data) != nil {
			logger.Warn("configuration reload rejected")
			continue
		}
		if version := g.ReloadStats().Version; version != before {
			logger.Info("configuration reloaded", "version", version)
		}
	}
}
