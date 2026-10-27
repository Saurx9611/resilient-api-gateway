// Package ratelimit performs approximate, atomic admission on a single Redis primary.
package ratelimit

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"errors"
	"fmt"
	"net/netip"
	"net/url"
	"regexp"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/redis/go-redis/v9/maintnotifications"
)

//go:embed window.lua
var windowScript string

var safeToken = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,32}$`)

// Policy is copied into the immutable route. FailOpen only applies to dependency
// failures, never identity errors or a successful Redis rejection.
type Policy struct {
	Limit    int64
	Window   time.Duration
	FailOpen bool
}

func DefaultPolicy() Policy { return Policy{Limit: 60, Window: time.Minute} }

func (p Policy) Validate() error {
	if p.Limit < 1 || p.Limit > 1_000_000 || p.Window < time.Second || p.Window > time.Hour || p.Window%time.Millisecond != 0 {
		return errors.New("rate policy requires limit 1..1000000 and whole-millisecond window 1s..1h")
	}
	return nil
}

type Config struct {
	URL           string
	Namespace     string
	Timeout       time.Duration
	MaxIdentities int
}

type Decision struct {
	Allowed    bool
	Capacity   bool
	Remaining  int64
	Reset      time.Duration
	RetryAfter time.Duration
}

type Limiter struct {
	client    *redis.Client
	script    *redis.Script
	namespace string
	timeout   time.Duration
	capacity  int
}

// New creates one bounded pool. It deliberately does not require Redis to be up
// at startup: each route's explicit outage policy applies to the first request too.
func New(cfg Config) (*Limiter, error) {
	if cfg.Namespace == "" {
		cfg.Namespace = "gateway"
	}
	if cfg.Timeout == 0 {
		cfg.Timeout = 100 * time.Millisecond
	}
	if cfg.MaxIdentities == 0 {
		cfg.MaxIdentities = 1024
	}
	if !safeToken.MatchString(cfg.Namespace) || cfg.Timeout < 5*time.Millisecond || cfg.Timeout > time.Second || cfg.MaxIdentities < 1 || cfg.MaxIdentities > 4096 {
		return nil, errors.New("invalid limiter namespace, timeout (5ms..1s), or identity cap (1..4096)")
	}
	u, err := url.Parse(cfg.URL)
	if err != nil || u.Hostname() == "" || (u.Scheme != "redis" && u.Scheme != "rediss") || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("invalid REDIS_URL")
	}
	opts, err := redis.ParseURL(cfg.URL)
	if err != nil {
		return nil, errors.New("invalid REDIS_URL")
	}
	opts.Protocol = 2
	opts.MaxRetries = -1
	opts.DialerRetries = 1
	opts.ContextTimeoutEnabled = true
	opts.DialTimeout, opts.ReadTimeout, opts.WriteTimeout = cfg.Timeout, cfg.Timeout, cfg.Timeout
	opts.PoolTimeout = cfg.Timeout
	opts.PoolSize, opts.MaxActiveConns, opts.MinIdleConns = 16, 16, 0
	opts.MaxIdleConns = 16
	opts.ConnMaxIdleTime, opts.ConnMaxLifetime = time.Minute, 5*time.Minute
	opts.DisableIdentity = true
	opts.MaintNotificationsConfig = &maintnotifications.Config{Mode: maintnotifications.ModeDisabled}
	return &Limiter{client: redis.NewClient(opts), script: redis.NewScript(windowScript), namespace: cfg.Namespace, timeout: cfg.Timeout, capacity: cfg.MaxIdentities}, nil
}

func (l *Limiter) Close() error { return l.client.Close() }

// Health consumes no quota or identity slots. It verifies reachability/auth,
// not scripting ACLs, distributed policy agreement or successful admission.
func (l *Limiter) Health(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, l.timeout)
	defer cancel()
	return l.client.Ping(ctx).Err()
}

// PeerID accepts socket IP:port only, without reading any forwarding header.
func PeerID(remoteAddr string) (string, error) {
	peer, err := netip.ParseAddrPort(remoteAddr)
	if err != nil {
		return "", errors.New("invalid peer address")
	}
	digest := sha256.Sum256([]byte(peer.Addr().WithZone("").Unmap().String()))
	return hex.EncodeToString(digest[:]), nil
}

func (l *Limiter) keys(route, id string) []string {
	prefix := "rag:rl:v1:{" + l.namespace + ":" + route + "}"
	return []string{prefix + ":peer:" + id, prefix + ":identities", prefix + ":policy"}
}

// Check has one total deadline, including pool acquisition and NOSCRIPT fallback.
// A timeout may have consumed quota; it is never retried or refunded.
func (l *Limiter) Check(ctx context.Context, route, identity string, policy Policy) (Decision, error) {
	if err := policy.Validate(); err != nil {
		return Decision{}, err
	}
	if !safeToken.MatchString(route) || len(identity) != 64 {
		return Decision{}, errors.New("invalid limiter route or identity")
	}
	if _, err := hex.DecodeString(identity); err != nil {
		return Decision{}, errors.New("invalid limiter identity")
	}
	ctx, cancel := context.WithTimeout(ctx, l.timeout)
	defer cancel()
	values, err := l.script.Run(ctx, l.client, l.keys(route, identity), policy.Limit, policy.Window.Milliseconds(), l.capacity, identity).Int64Slice()
	if err != nil {
		return Decision{}, err
	}
	if len(values) != 5 || values[0] < 0 || values[0] > 2 || values[1] < 0 || values[1] > policy.Limit || values[2] < 0 || values[2] > 2*policy.Window.Milliseconds() || values[3] < 0 || values[3] > 2*policy.Window.Milliseconds() {
		return Decision{}, fmt.Errorf("invalid Redis admission response")
	}
	return Decision{Allowed: values[0] == 1, Capacity: values[0] == 2, Remaining: values[1], Reset: time.Duration(values[2]) * time.Millisecond, RetryAfter: time.Duration(values[3]) * time.Millisecond}, nil
}
