// Command gateway serves two static, operator-configured upstream routes.
package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"log/slog"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"example.com/resilient-api-gateway/internal/breaker"
	"example.com/resilient-api-gateway/internal/gateway"
	"example.com/resilient-api-gateway/internal/ratelimit"
	"example.com/resilient-api-gateway/internal/server"
)

func run(ctx context.Context, args []string, out io.Writer) error {
	flags := flag.NewFlagSet("gateway", flag.ContinueOnError)
	flags.SetOutput(out)
	listen := flags.String("listen", envOr("GATEWAY_LISTEN", "127.0.0.1:8080"), "HTTP listen address")
	oneDefault, err := originFromEnv("UPSTREAM_ONE_HOSTPORT", "http://127.0.0.1:9001")
	if err != nil {
		return err
	}
	twoDefault, err := originFromEnv("UPSTREAM_TWO_HOSTPORT", "http://127.0.0.1:9002")
	if err != nil {
		return err
	}
	one := flags.String("upstream-one", oneDefault, "operator allowlist origin for /one")
	two := flags.String("upstream-two", twoDefault, "operator allowlist origin for /two")
	limitOne := flags.Int64("limit-one", 60, "unit requests per window for /one")
	limitTwo := flags.Int64("limit-two", 60, "unit requests per window for /two")
	window := flags.Duration("rate-window", time.Minute, "weighted-counter window (1s..1h)")
	modeOne := flags.String("redis-failure-one", "closed", "Redis outage policy for /one: closed or open")
	modeTwo := flags.String("redis-failure-two", "closed", "Redis outage policy for /two: closed or open")
	timeout := flags.Duration("redis-timeout", 100*time.Millisecond, "total Redis admission budget (5ms..1s)")
	cap := flags.Int("identity-cap", 1024, "maximum active peer identities per route (1..4096)")
	namespace := flags.String("rate-namespace", "gateway", "shared Redis namespace; bounded safe token")
	threshold := flags.Int("circuit-failures", 5, "consecutive upstream failures to open circuit (1..10000)")
	openFor := flags.Duration("circuit-open", 5*time.Second, "per-instance circuit open interval (1ms..1h)")
	probes := flags.Int("circuit-probes", 1, "maximum simultaneous half-open probes (1..64)")
	recovery := flags.Int("circuit-recovery", 2, "successful half-open probes required (1..10000)")
	configPath := flags.String("config", "", "operator JSON route config; enables local file polling")
	reloadInterval := flags.Duration("reload-interval", time.Second, "config polling interval (100ms..1m)")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return errors.New("invalid command-line flags")
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	metricsToken := os.Getenv("METRICS_BEARER_TOKEN")
	demoToken := os.Getenv("GATEWAY_DEMO_TOKEN")
	if metricsToken != "" && !validBearerToken(metricsToken) {
		return errors.New("METRICS_BEARER_TOKEN must be a 32..256 byte bearer token")
	}
	if demoToken != "" && !validBearerToken(demoToken) {
		return errors.New("GATEWAY_DEMO_TOKEN must be a 32..256 byte bearer token")
	}
	if metricsToken != "" && metricsToken == demoToken {
		return errors.New("metrics and demo tokens must be distinct")
	}
	if !loopbackListen(*listen) && metricsToken == "" {
		return errors.New("METRICS_BEARER_TOKEN is required for a non-loopback listener")
	}
	logger := slog.New(slog.NewJSONHandler(out, nil))
	if *reloadInterval < 100*time.Millisecond || *reloadInterval > time.Minute {
		return errors.New("invalid reload interval (100ms..1m)")
	}
	for _, mode := range []string{*modeOne, *modeTwo} {
		if mode != "closed" && mode != "open" {
			return errors.New("Redis failure policy must be closed or open")
		}
	}
	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		redisURL = "redis://127.0.0.1:6379/0"
	}
	limiter, err := ratelimit.New(ratelimit.Config{URL: redisURL, Namespace: *namespace, Timeout: *timeout, MaxIdentities: *cap})
	if err != nil {
		return err
	}
	defer limiter.Close()
	cfg := gateway.Config{Targets: [2]string{*one, *two}, RedisFailure: [2]string{*modeOne, *modeTwo}, Readiness: "required"}
	var initial []byte
	if *configPath != "" {
		initial, err = gateway.ReadConfigFile(*configPath)
		if err != nil {
			return err
		}
		cfg, err = gateway.DecodeConfig(initial)
		if err != nil {
			return err
		}
	}
	proxy, err := gateway.NewConfigured(cfg, gateway.Options{Logger: logger, Limiter: limiter, RedisHealth: limiter.Health, CircuitPolicy: breaker.Policy{FailureThreshold: *threshold, OpenDuration: *openFor, MaxProbes: *probes, RecoverySuccesses: *recovery}, RatePolicies: [2]ratelimit.Policy{
		{Limit: *limitOne, Window: *window, FailOpen: *modeOne == "open"},
		{Limit: *limitTwo, Window: *window, FailOpen: *modeTwo == "open"},
	}, DemoToken: demoToken})
	if err != nil {
		return err
	}
	defer proxy.CloseIdleConnections()
	defer func() { logger.Info("circuit summary", "routes", proxy.CircuitStats()) }()
	defer func() { logger.Info("admission summary", "routes", proxy.AdmissionStats()) }()
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		return errors.New("cannot bind HTTP listener")
	}
	logger.Info("gateway listening", "address", ln.Addr().String())
	if *configPath != "" {
		watchCtx, stop := context.WithCancel(ctx)
		done := make(chan struct{})
		go func() { defer close(done); watchConfig(watchCtx, *configPath, *reloadInterval, proxy, initial, logger) }()
		defer func() { stop(); <-done }()
	}
	defer func() { logger.Info("configuration summary", "reload", proxy.ReloadStats()) }()
	return server.NewWithMetricsToken(proxy, metricsToken).Serve(ctx, ln, 10*time.Second, logger)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func validBearerToken(token string) bool {
	if len(token) < 32 || len(token) > 256 {
		return false
	}
	padding := false
	for _, c := range []byte(token) {
		if c == '=' {
			padding = true
			continue
		}
		if padding || !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || strings.ContainsRune("-._~+/", rune(c))) {
			return false
		}
	}
	return true
}

func loopbackListen(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func originFromEnv(key, fallback string) (string, error) {
	hostport := os.Getenv(key)
	if hostport == "" {
		return fallback, nil
	}
	host, port, err := net.SplitHostPort(hostport)
	portNumber, portErr := strconv.Atoi(port)
	if err != nil || host == "" || port == "" || portErr != nil || portNumber < 1 || portNumber > 65535 {
		return "", errors.New("invalid upstream hostport configuration")
	}
	u, err := url.Parse("http://" + hostport)
	if err != nil || u.Host != hostport || u.User != nil || u.Path != "" {
		return "", errors.New("invalid upstream hostport configuration")
	}
	return "http://" + hostport, nil
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Args[1:], os.Stderr); err != nil {
		slog.Error("gateway stopped", "error", err)
		os.Exit(1)
	}
}
