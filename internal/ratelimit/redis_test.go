package ratelimit

import (
	"context"
	"fmt"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func realLimiter(t *testing.T) *Limiter {
	t.Helper()
	endpoint := os.Getenv("REDIS_TEST_URL")
	if endpoint == "" {
		t.Skip("set REDIS_TEST_URL for real Redis tests")
	}
	l, err := New(Config{URL: endpoint, Namespace: fmt.Sprintf("test%d", time.Now().UnixNano()), Timeout: time.Second, MaxIdentities: 2})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	if err := l.client.Ping(context.Background()).Err(); err != nil {
		t.Fatal(err)
	}
	return l
}

func TestRedisBoundaries(t *testing.T) {
	l := realLimiter(t)
	ctx := context.Background()
	id, _ := PeerID("192.0.2.1:1")
	keys := l.keys("one", id)
	// Replace only TIME in a private script copy; all state operations run in Redis.
	script := strings.Replace(windowScript, "local timestamp = redis.call('TIME')", "local timestamp = {ARGV[5], ARGV[6]}", 1)
	base := time.Now().UnixMilli() / 1000 * 1000
	check := func(offset int64) []int64 {
		t.Helper()
		now := base + offset
		v, err := l.client.Eval(ctx, script, keys, 2, 1000, 2, id, now/1000, (now%1000)*1000).Int64Slice()
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	if v := check(0); v[0] != 1 || v[1] != 1 || v[2] != 2000 {
		t.Fatal(v)
	}
	if v := check(0); v[0] != 1 || v[1] != 0 {
		t.Fatal(v)
	}
	expiry, err := l.client.PExpireTime(ctx, keys[0]).Result()
	if err != nil {
		t.Fatal(err)
	}
	if v := check(999); v[0] != 0 || v[3] != 501 {
		t.Fatal(v)
	}
	after, _ := l.client.PExpireTime(ctx, keys[0]).Result()
	if after != expiry {
		t.Fatal("denial extended TTL")
	}
	if v := check(1000); v[0] != 0 || v[3] != 500 || v[2] != 1000 {
		t.Fatal(v)
	}
	if v := check(1499); v[0] != 0 || v[3] != 1 {
		t.Fatal(v)
	}
	if v := check(1500); v[0] != 1 || v[1] != 0 {
		t.Fatal(v)
	}
	if v := check(4000); v[0] != 1 || v[1] != 1 {
		t.Fatal(v)
	}
	if _, err := l.client.Eval(ctx, script, keys, 2, 1000, 2, id, (base+3000)/1000, 0).Result(); err == nil {
		t.Fatal("rollback accepted")
	}
	// A late prior-bucket burst is underweighted: three admissions within an
	// exact 1s interval are possible with limit 2. Preserve this explicit limit
	// of the approximation instead of asserting an exact rolling ceiling.
	otherKeys := l.keys("two", id)
	for _, offset := range []int64{999, 999, 1500} {
		now := base + offset
		v, err := l.client.Eval(ctx, script, otherKeys, 2, 1000, 2, id, now/1000, (now%1000)*1000).Int64Slice()
		if err != nil || v[0] != 1 {
			t.Fatalf("approximation example %v %v", v, err)
		}
	}
	t.Log("real Redis: boundary, decay rounding, TTL, idle gap and rollback checks passed")
}

func TestRedisServerTimeContentionAndCapacity(t *testing.T) {
	l := realLimiter(t)
	ctx := context.Background()
	id, _ := PeerID("192.0.2.1:1")
	p := Policy{Limit: 20, Window: time.Hour}
	before, err := l.client.Time(ctx).Result()
	if err != nil {
		t.Fatal(err)
	}
	var admitted atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 80; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			d, e := l.Check(ctx, "one", id, p)
			if e != nil {
				t.Error(e)
			}
			if d.Allowed {
				admitted.Add(1)
			}
		}()
	}
	wg.Wait()
	if admitted.Load() != 20 {
		t.Fatalf("admitted=%d", admitted.Load())
	}
	after, _ := l.client.Time(ctx).Result()
	expiry, err := l.client.PExpireTime(ctx, l.keys("one", id)[0]).Result()
	if err != nil {
		t.Fatal(err)
	}
	lo := (before.UnixMilli()/p.Window.Milliseconds() + 2) * p.Window.Milliseconds()
	hi := (after.UnixMilli()/p.Window.Milliseconds() + 2) * p.Window.Milliseconds()
	if expiry.Milliseconds() < lo || expiry.Milliseconds() > hi {
		t.Fatal("expiry not based on Redis TIME", expiry)
	}
	for i := 2; i <= 3; i++ {
		other, _ := PeerID(fmt.Sprintf("192.0.2.%d:1", i))
		d, e := l.Check(ctx, "one", other, p)
		if e != nil || d.Capacity != (i == 3) {
			t.Fatalf("capacity %v %v", d, e)
		}
	}
	if _, e := l.Check(ctx, "one", id, Policy{Limit: 21, Window: time.Hour}); e == nil {
		t.Fatal("policy mismatch accepted")
	}
	t.Logf("real Redis concurrent admissions=%d/80; server-time expiry and identity cap verified", admitted.Load())
}

func TestRedisLatencyBudget(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			c, e := ln.Accept()
			if e != nil {
				return
			}
			go func() { defer c.Close(); <-done }()
		}
	}()
	l, e := New(Config{URL: "redis://" + ln.Addr().String(), Timeout: 30 * time.Millisecond})
	if e != nil {
		t.Fatal(e)
	}
	defer l.Close()
	id, _ := PeerID("192.0.2.1:1")
	start := time.Now()
	_, e = l.Check(context.Background(), "one", id, DefaultPolicy())
	elapsed := time.Since(start)
	if e == nil || elapsed > 250*time.Millisecond {
		t.Fatalf("budget elapsed=%s error=%v", elapsed, e)
	}
	t.Logf("stalled Redis admission returned in %s (30ms configured; 250ms scheduling tolerance)", elapsed)
}

func TestRedisExpiryAndCapacityRecovery(t *testing.T) {
	l := realLimiter(t)
	ctx := context.Background()
	p := Policy{Limit: 1, Window: time.Second}
	for i := 1; i <= 2; i++ {
		id, _ := PeerID(fmt.Sprintf("192.0.2.%d:1", i))
		if d, e := l.Check(ctx, "one", id, p); e != nil || !d.Allowed {
			t.Fatalf("%v %v", d, e)
		}
	}
	id, _ := PeerID("192.0.2.3:1")
	if d, e := l.Check(ctx, "one", id, p); e != nil || !d.Capacity {
		t.Fatalf("%v %v", d, e)
	}
	time.Sleep(2100 * time.Millisecond)
	if d, e := l.Check(ctx, "one", id, p); e != nil || !d.Allowed {
		t.Fatalf("expiry recovery %v %v", d, e)
	}
	old, _ := PeerID("192.0.2.1:1")
	if n, e := l.client.Exists(ctx, l.keys("one", old)[0]).Result(); e != nil || n != 0 {
		t.Fatal("old state retained", n, e)
	}
}
