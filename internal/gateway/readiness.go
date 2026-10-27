package gateway

import (
	"context"
	"errors"
	"time"
)

// Ready checks one snapshot. It does not globally gate requests: each selected
// route still applies its own outage mode. PING does not prove Lua permissions.
func (g *Gateway) Ready(ctx context.Context) error {
	selected := g.current.Load()
	if g.admission == nil {
		return nil
	}
	if selected.config.Readiness == "policy" && selected.routes[0].policy.FailOpen && selected.routes[1].policy.FailOpen {
		return nil
	}
	if g.opts.RedisHealth == nil {
		return errors.New("Redis health check unavailable")
	}
	// Bound even a caller without a deadline; the real limiter may use a shorter
	// configured timeout. Health implementations must honor context cancellation.
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := g.opts.RedisHealth(ctx); err != nil {
		return errors.New("Redis unavailable")
	}
	if ctx.Err() != nil {
		return errors.New("Redis health deadline exceeded")
	}
	return nil
}
