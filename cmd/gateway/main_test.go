package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestHelp(t *testing.T) {
	var out bytes.Buffer
	if err := run(context.Background(), []string{"-h"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "upstream-one") {
		t.Fatal("missing upstream flag help")
	}
}

func TestInvalidStartup(t *testing.T) {
	for _, args := range [][]string{{"extra"}, {"-upstream-one", "http://user:secret@localhost"}} {
		var out bytes.Buffer
		err := run(context.Background(), args, &out)
		if err == nil || strings.Contains(err.Error()+out.String(), "secret") {
			t.Fatalf("invalid startup error = %v, output = %q", err, out.String())
		}
	}
}

func TestListenerAndUpstreamDeploymentValidation(t *testing.T) {
	t.Setenv("METRICS_BEARER_TOKEN", "")
	var out bytes.Buffer
	if err := run(context.Background(), []string{"-listen", "0.0.0.0:8080"}, &out); err == nil || !strings.Contains(err.Error(), "required for a non-loopback") {
		t.Fatalf("wildcard listener without metrics token: %v", err)
	}
	t.Setenv("METRICS_BEARER_TOKEN", "short")
	if err := run(context.Background(), []string{"-listen", "0.0.0.0:8080"}, &out); err == nil || !strings.Contains(err.Error(), "32..256") {
		t.Fatalf("short metrics token: %v", err)
	}
	t.Setenv("METRICS_BEARER_TOKEN", strings.Repeat("x", 31)+"\n")
	if err := run(context.Background(), []string{"-listen", "0.0.0.0:8080"}, &out); err == nil || !strings.Contains(err.Error(), "bearer token") {
		t.Fatalf("invalid metrics token characters: %v", err)
	}
	t.Setenv("METRICS_BEARER_TOKEN", strings.Repeat("x", 32))
	t.Setenv("UPSTREAM_ONE_HOSTPORT", "")
	t.Setenv("UPSTREAM_TWO_HOSTPORT", "")
	if loopbackListen("127.0.0.1:8080") != true || loopbackListen("[::1]:8080") != true || loopbackListen("0.0.0.0:8080") || loopbackListen(":8080") {
		t.Fatal("listener loopback classification changed")
	}
	for _, hostport := range []string{"http://example:80", "example:80/path", "user@host:80", "example:no", "example:65536", "bad"} {
		t.Setenv("UPSTREAM_ONE_HOSTPORT", hostport)
		if _, err := originFromEnv("UPSTREAM_ONE_HOSTPORT", "http://127.0.0.1:9001"); err == nil {
			t.Fatalf("accepted unsafe hostport %q", hostport)
		}
	}
	t.Setenv("UPSTREAM_ONE_HOSTPORT", "")
	t.Setenv("UPSTREAM_TWO_HOSTPORT", "")
	t.Setenv("GATEWAY_DEMO_TOKEN", strings.Repeat("x", 32))
	if err := run(context.Background(), []string{"-listen", "0.0.0.0:8080"}, &out); err == nil || !strings.Contains(err.Error(), "must be distinct") {
		t.Fatalf("reused token: %v", err)
	}
}
