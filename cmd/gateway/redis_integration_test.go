package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"sync"
	"testing"
	"time"
)

func TestGatewayRedisProcess(t *testing.T) {
	if os.Getenv("GATEWAY_TEST_CHILD") != "1" {
		return
	}
	if err := run(context.Background(), []string{"-listen", os.Getenv("GATEWAY_TEST_ADDR"), "-upstream-one", os.Getenv("GATEWAY_TEST_UPSTREAM"), "-upstream-two", os.Getenv("GATEWAY_TEST_UPSTREAM"), "-rate-namespace", os.Getenv("GATEWAY_TEST_NAMESPACE"), "-limit-one", "20", "-rate-window", "1h", "-redis-timeout", "1s"}, io.Discard); err != nil {
		t.Fatal(err)
	}
}

func TestRedisTwoGatewayProcesses(t *testing.T) {
	endpoint := os.Getenv("REDIS_TEST_URL")
	if endpoint == "" {
		t.Skip("set REDIS_TEST_URL for real Redis process test")
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer upstream.Close()
	namespace := fmt.Sprintf("process%d", time.Now().UnixNano())
	client := &http.Client{Timeout: 3 * time.Second}
	defer client.CloseIdleConnections()
	var urls [2]string
	for i := range urls {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		addr := ln.Addr().String()
		ln.Close()
		cmd := exec.Command(os.Args[0], "-test.run=^TestGatewayRedisProcess$")
		cmd.Env = append(os.Environ(), "GATEWAY_TEST_CHILD=1", "GATEWAY_TEST_ADDR="+addr, "GATEWAY_TEST_UPSTREAM="+upstream.URL, "GATEWAY_TEST_NAMESPACE="+namespace, "REDIS_URL="+endpoint)
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
		urls[i] = "http://" + addr
		ready := false
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			res, e := client.Get(urls[i] + "/healthz")
			if e == nil {
				res.Body.Close()
				ready = res.StatusCode == 200
				if ready {
					break
				}
			}
			time.Sleep(20 * time.Millisecond)
		}
		if !ready {
			t.Fatal("gateway child not ready")
		}
	}
	var admitted [2]int
	var mu sync.Mutex
	request := func(i int) {
		t.Helper()
		req, _ := http.NewRequest("GET", urls[i]+"/one", nil)
		req.Header.Set("X-Forwarded-For", fmt.Sprintf("192.0.2.%d", time.Now().UnixNano()%250+1))
		res, e := client.Do(req)
		if e != nil {
			t.Error(e)
			return
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
		if res.Header.Get("RateLimit-Limit") != "20" {
			t.Error("missing quota header")
		}
		switch res.StatusCode {
		case 200:
			mu.Lock()
			admitted[i]++
			mu.Unlock()
		case 429:
			if res.Header.Get("Retry-After") == "" {
				t.Error("missing retry")
			}
		default:
			t.Errorf("unexpected status %d", res.StatusCode)
		}
	}
	// Ensure both OS processes participate before contention.
	request(0)
	request(1)
	var wg sync.WaitGroup
	for i := 0; i < 78; i++ {
		wg.Add(1)
		go func(i int) { defer wg.Done(); request(i % 2) }(i)
	}
	wg.Wait()
	if admitted[0]+admitted[1] != 20 || admitted[0] == 0 || admitted[1] == 0 {
		t.Fatalf("admitted per process=%v", admitted)
	}
	t.Logf("two independent OS processes: admissions=%v total=20; 60 HTTP 429; spoofed forwarding identities ignored", admitted)
}
