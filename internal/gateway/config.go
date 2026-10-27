package gateway

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
)

const MaxConfigBytes = 64 << 10

// ReadConfigFile accepts operator-controlled regular files, never a request path.
// Replace files atomically. The size/read bound is enforced even if Stat races.
func ReadConfigFile(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > MaxConfigBytes {
		return nil, errors.New("cannot read configuration file")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, errors.New("cannot read configuration file")
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, MaxConfigBytes+1))
	if err != nil || len(data) > MaxConfigBytes {
		return nil, errors.New("cannot read configuration file")
	}
	return data, nil
}

// DecodeConfig rejects unknown/duplicate fields, extra documents and wrong array
// lengths. Detailed decoder errors may contain secrets, so never return them.
func DecodeConfig(data []byte) (Config, error) {
	invalid := errors.New("invalid configuration document")
	if len(data) > MaxConfigBytes {
		return Config{}, invalid
	}
	// Check object keys explicitly because encoding/json otherwise accepts duplicates.
	dec := json.NewDecoder(bytes.NewReader(data))
	token, err := dec.Token()
	if err != nil || token != json.Delim('{') {
		return Config{}, invalid
	}
	fields := map[string]json.RawMessage{}
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			return Config{}, invalid
		}
		name, ok := key.(string)
		if !ok {
			return Config{}, invalid
		}
		if _, exists := fields[name]; exists {
			return Config{}, invalid
		}
		if name != "targets" && name != "redis_failure" && name != "readiness" {
			return Config{}, invalid
		}
		var raw json.RawMessage
		if dec.Decode(&raw) != nil {
			return Config{}, invalid
		}
		fields[name] = raw
	}
	if _, err := dec.Token(); err != nil {
		return Config{}, invalid
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return Config{}, invalid
	}
	var targets, modes []string
	if json.Unmarshal(fields["targets"], &targets) != nil || len(targets) != 2 {
		return Config{}, invalid
	}
	if json.Unmarshal(fields["redis_failure"], &modes) != nil || len(modes) != 2 {
		return Config{}, invalid
	}
	readiness := "required"
	if raw, ok := fields["readiness"]; ok {
		if string(raw) == "null" || json.Unmarshal(raw, &readiness) != nil || readiness == "" {
			return Config{}, invalid
		}
	}
	cfg := Config{Targets: [2]string{targets[0], targets[1]}, RedisFailure: [2]string{modes[0], modes[1]}, Readiness: readiness}
	// URL and policy validation happens in buildSnapshot before publication.
	return cfg, nil
}

// NewConfigured uses the same validated builder without counting startup as reload.
func NewConfigured(cfg Config, opts Options) (*Gateway, error) {
	g, err := New(cfg.Targets, opts)
	if err != nil {
		return nil, err
	}
	initial, err := g.buildSnapshot(cfg, g.current.Load())
	if err != nil {
		g.CloseIdleConnections()
		return nil, err
	}
	initial.version = 1
	g.current.Store(initial)
	return g, nil
}

func (g *Gateway) ReloadJSON(data []byte) error {
	cfg, err := DecodeConfig(data)
	if err != nil {
		g.RecordReloadFailure()
		return err
	}
	return g.Reload(cfg)
}

// RecordReloadFailure covers file read errors before a candidate can be built.
func (g *Gateway) RecordReloadFailure() { g.reloadMu.Lock(); g.rejected++; g.reloadMu.Unlock() }
