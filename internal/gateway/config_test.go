package gateway_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"example.com/resilient-api-gateway/internal/gateway"
)

func TestConfigDocumentValidationAndLastGood(t *testing.T) {
	good := `{"targets":["http://127.0.0.1:1","http://127.0.0.1:2"],"redis_failure":["closed","open"],"readiness":"required"}`
	cfg, err := gateway.DecodeConfig([]byte(good))
	if err != nil {
		t.Fatal(err)
	}
	g, err := gateway.NewConfigured(cfg, quietOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	tests := []string{
		"", `null`, `[]`, good + ` {}`, strings.Replace(good, `"required"`, `null`, 1),
		strings.Replace(good, `"readiness":"required"`, `"readiness":"required","readiness":"policy"`, 1),
		strings.Replace(good, `"targets"`, `"Targets"`, 1),
		strings.Replace(good, `"required"`, `"unknown"`, 1),
		strings.Replace(good, `"closed"`, `"fail-typo"`, 1),
		strings.Replace(good, `"http://127.0.0.1:1"`, `"http://user:secret@host"`, 1),
		strings.Replace(good, `"http://127.0.0.1:2"`, `"file:///etc/passwd"`, 1),
		strings.Replace(good, `"http://127.0.0.1:2"`, `"http://host/base"`, 1),
		strings.Replace(good, `"http://127.0.0.1:2"`, `"http://host:99999"`, 1),
		strings.Replace(good, `"http://127.0.0.1:2"`, `"http://host?target=x"`, 1),
		strings.Replace(good, `"http://127.0.0.1:2"`, `"http://host#fragment"`, 1),
		strings.Replace(good, `"http://127.0.0.1:1",`, "", 1),
		strings.Replace(good, `"http://127.0.0.1:2"]`, `"http://127.0.0.1:2","http://host:3"]`, 1),
		strings.Replace(good, `"closed","open"`, `"closed"`, 1),
		strings.Replace(good, `"closed","open"`, `"closed","open","closed"`, 1),
		strings.Replace(good, `"readiness"`, `"rate_window"`, 1),
		strings.Repeat(" ", gateway.MaxConfigBytes+1),
	}
	for i, bad := range tests {
		err := g.ReloadJSON([]byte(bad))
		if err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatalf("case %d accepted/leaked: %v", i, err)
		}
		if g.CurrentConfig() != cfg || g.ReloadStats().Version != 1 {
			t.Fatal("invalid document replaced config")
		}
	}
	if g.ReloadStats().Rejected != uint64(len(tests)) {
		t.Fatal(g.ReloadStats())
	}
	if err := g.ReloadJSON([]byte(good)); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "routes.json")
	if err := os.WriteFile(path, []byte(good), 0600); err != nil {
		t.Fatal(err)
	}
	if data, e := gateway.ReadConfigFile(path); e != nil || string(data) != good {
		t.Fatal("read failed", e)
	}
	if _, e := gateway.ReadConfigFile(filepath.Dir(path)); e == nil {
		t.Fatal("accepted directory")
	}
	if _, e := gateway.ReadConfigFile(path + "missing"); e == nil {
		t.Fatal("accepted missing file")
	}
	if e := os.WriteFile(path, []byte(strings.Repeat(" ", gateway.MaxConfigBytes+1)), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := gateway.ReadConfigFile(path); e == nil {
		t.Fatal("accepted oversized file")
	}
}
