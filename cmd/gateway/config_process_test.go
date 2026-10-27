package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestConfigFileGatewayProcess(t *testing.T) {
	if os.Getenv("CONFIG_TEST_CHILD") != "1" {
		return
	}
	if err := run(context.Background(), []string{"-listen", os.Getenv("CONFIG_TEST_ADDR"), "-config", os.Getenv("CONFIG_TEST_PATH"), "-reload-interval", "100ms", "-limit-one", "1000", "-limit-two", "1000", "-rate-namespace", os.Getenv("CONFIG_TEST_NAMESPACE")}, os.Stderr); err != nil {
		t.Fatal(err)
	}
}

func TestConfigCLIProcessReloadAndRestoration(t *testing.T) {
	endpoint := os.Getenv("REDIS_TEST_URL")
	if endpoint == "" {
		t.Skip("set REDIS_TEST_URL for config CLI process test")
	}
	makeUpstream := func(name string) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, name) }))
	}
	old, fresh := makeUpstream("old"), makeUpstream("new")
	defer old.Close()
	defer fresh.Close()
	dir := t.TempDir()
	path := filepath.Join(dir, "routes.json")
	write := func(data []byte) {
		t.Helper()
		if e := os.WriteFile(path+".tmp", data, 0600); e != nil {
			t.Fatal(e)
		}
		if e := os.Rename(path+".tmp", path); e != nil {
			t.Fatal(e)
		}
	}
	document := func(target string) []byte {
		data, e := json.Marshal(map[string]any{"targets": []string{target, target}, "redis_failure": []string{"closed", "open"}, "readiness": "required"})
		if e != nil {
			t.Fatal(e)
		}
		return data
	}
	write(document(old.URL))
	ln, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	addr := ln.Addr().String()
	ln.Close()
	logPath := filepath.Join(dir, "gateway.log")
	log, e := os.Create(logPath)
	if e != nil {
		t.Fatal(e)
	}
	defer log.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^TestConfigFileGatewayProcess$")
	cmd.Env = append(os.Environ(), "CONFIG_TEST_CHILD=1", "CONFIG_TEST_ADDR="+addr, "CONFIG_TEST_PATH="+path, "CONFIG_TEST_NAMESPACE="+fmt.Sprintf("file%d", time.Now().UnixNano()), "REDIS_URL="+endpoint)
	cmd.Stdout = log
	cmd.Stderr = log
	if e := cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer func() { cmd.Process.Kill(); cmd.Wait() }()
	client := &http.Client{Timeout: time.Second}
	defer client.CloseIdleConnections()
	base := "http://" + addr
	await := func(check func() bool) {
		t.Helper()
		deadline := time.Now().Add(4 * time.Second)
		for !check() {
			if time.Now().After(deadline) {
				data, _ := os.ReadFile(logPath)
				t.Fatalf("process reload timed out: %s", data)
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	fetch := func(path string) (int, string) {
		res, e := client.Get(base + path)
		if e != nil {
			return 0, ""
		}
		defer res.Body.Close()
		data, e := io.ReadAll(res.Body)
		if e != nil {
			return 0, ""
		}
		return res.StatusCode, string(data)
	}
	await(func() bool { status, _ := fetch("/readyz"); return status == 200 })
	if status, body := fetch("/one"); status != 200 || body != "old" {
		t.Fatal(status, body)
	}
	write(document("http://user:secret@invalid"))
	await(func() bool {
		data, _ := os.ReadFile(logPath)
		return strings.Contains(string(data), "configuration reload rejected")
	})
	if status, body := fetch("/one"); status != 200 || body != "old" {
		t.Fatal("invalid config displaced old", status, body)
	}
	write(document(fresh.URL))
	await(func() bool { status, body := fetch("/two"); return status == 200 && body == "new" })
	if status, _ := fetch("/readyz"); status != 200 {
		t.Fatal("valid restoration not ready", status)
	}
	if status, _ := fetch("/admin/reload"); status != 404 {
		t.Fatal("unexpected public reload API", status)
	}
	data, _ := os.ReadFile(logPath)
	if strings.Contains(string(data), "secret") {
		t.Fatal("configuration leaked in logs")
	}
	t.Log("real Redis and native gateway process: file polling rejected unsafe target, retained old route, restored new route, readiness 200; no public reload endpoint")
}
