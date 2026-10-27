package gateway_test

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
)

func startProxy(t *testing.T, targets [2]string, opts gateway.Options) *httptest.Server {
	t.Helper()
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	g, err := gateway.New(targets, opts)
	if err != nil {
		t.Fatal(err)
	}
	s := httptest.NewServer(g)
	s.Client().Timeout = 4 * time.Second
	s.Client().CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	t.Cleanup(func() { s.Close(); g.CloseIdleConnections() })
	return s
}

func upstream(t *testing.T, fn http.HandlerFunc) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(fn)
	t.Cleanup(s.Close)
	return s
}

func readBody(t *testing.T, res *http.Response) string {
	t.Helper()
	defer res.Body.Close()
	b, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func await(t *testing.T, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for synchronized HTTP event")
	}
}

func TestForwardingAndStaticRoutes(t *testing.T) {
	type observed struct {
		method, path, rawPath, query, host, body string
		header                                   http.Header
	}
	seen := make(chan observed, 1)
	makeUpstream := func(name string) *httptest.Server {
		return upstream(t, func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			seen <- observed{r.Method, r.URL.Path, r.URL.EscapedPath(), r.URL.RawQuery, r.Host, string(body), r.Header.Clone()}
			w.Header().Set("X-Upstream", name)
			w.Header().Add("Set-Cookie", "a=1")
			w.Header().Add("Set-Cookie", "b=2")
			w.Header().Set("Trailer", "X-Checksum")
			w.WriteHeader(http.StatusCreated)
			io.WriteString(w, "response")
			w.Header().Set("X-Checksum", "complete")
		})
	}
	one, two := makeUpstream("one"), makeUpstream("two")
	proxy := startProxy(t, [2]string{one.URL, two.URL}, gateway.Options{})
	query := "q=a+b&q=a%20b&empty=&encoded=%2F&target=http%3A%2F%2Fevil.invalid"
	for _, rt := range []struct{ prefix, name, target string }{{"/one", "one", one.URL}, {"/two", "two", two.URL}} {
		for _, method := range []string{"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} {
			t.Run(rt.name+"/"+method, func(t *testing.T) {
				req, _ := http.NewRequest(method, proxy.URL+rt.prefix+"/caf%C3%A9/space%20here?"+query, strings.NewReader("request-body"))
				req.Host = "public.example:8080"
				req.Header.Set("X-End-To-End", "kept")
				req.Header.Set("Authorization", "Bearer controlled-test-value")
				res, err := proxy.Client().Do(req)
				if err != nil {
					t.Fatal(err)
				}
				body := readBody(t, res)
				if res.StatusCode != 201 || res.Header.Get("X-Upstream") != rt.name || len(res.Header.Values("Set-Cookie")) != 2 {
					t.Fatalf("response = %d %v", res.StatusCode, res.Header)
				}
				if (method == "HEAD" && body != "") || (method != "HEAD" && (body != "response" || res.Trailer.Get("X-Checksum") != "complete")) {
					t.Fatalf("body = %q, trailers = %v", body, res.Trailer)
				}
				got := <-seen
				target, _ := url.Parse(rt.target)
				if got.method != method || got.path != "/café/space here" || got.rawPath != "/caf%C3%A9/space%20here" || got.query != query || got.host != target.Host || got.body != "request-body" {
					t.Fatalf("upstream saw %+v", got)
				}
				if got.header.Get("X-End-To-End") != "kept" || got.header.Get("Authorization") != "Bearer controlled-test-value" {
					t.Fatalf("end-to-end headers = %v", got.header)
				}
			})
		}
	}
}

func TestForwardingTrustAndHopHeaders(t *testing.T) {
	seen := make(chan http.Header, 1)
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Clone()
		w.Header().Set("Connection", "X-Private")
		w.Header().Set("X-Private", "must disappear")
		w.Header().Set("Proxy-Authenticate", "must disappear")
		w.Header().Set("X-Public", "kept")
		io.WriteString(w, "ok")
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	req, _ := http.NewRequest("GET", p.URL+"/one", nil)
	req.Host = "public.example"
	for _, name := range []string{"Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Port", "X-Forwarded-Prefix", "X-Real-IP", "X-Hop", "Keep-Alive", "Proxy-Authorization", "Proxy-Connection"} {
		req.Header.Add(name, "spoofed")
	}
	req.Header.Add("X-Forwarded-For", "second-spoof")
	req.Header.Set("Connection", "X-Hop, X-Forwarded-For, X-Forwarded-Proto")
	res, err := p.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	got := <-seen
	if got.Get("X-Forwarded-For") != "127.0.0.1" || got.Get("X-Forwarded-Host") != "public.example" || got.Get("X-Forwarded-Proto") != "http" {
		t.Fatalf("rebuilt metadata = %v", got)
	}
	for _, name := range []string{"Forwarded", "X-Forwarded-Port", "X-Forwarded-Prefix", "X-Real-IP", "X-Hop", "Connection", "Keep-Alive", "Proxy-Authorization", "Proxy-Connection"} {
		if got.Get(name) != "" {
			t.Errorf("upstream received %s: %q", name, got.Get(name))
		}
	}
	if res.Header.Get("X-Private") != "" || res.Header.Get("Proxy-Authenticate") != "" || res.Header.Get("X-Public") != "kept" {
		t.Fatalf("response hop headers = %v", res.Header)
	}
}

func TestActualTLSForwardingProto(t *testing.T) {
	seen := make(chan string, 1)
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { seen <- r.Header.Get("X-Forwarded-Proto") })
	g, err := gateway.New([2]string{u.URL, u.URL}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewTLSServer(g)
	defer p.Close()
	p.Client().Timeout = 4 * time.Second
	req, _ := http.NewRequestWithContext(context.Background(), "GET", p.URL+"/one", nil)
	req.Header.Set("X-Forwarded-Proto", "http")
	res, err := p.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if proto := <-seen; proto != "https" {
		t.Fatalf("forwarded proto = %q", proto)
	}
}

func TestRouteBoundariesAndMalformedInputs(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, r.URL.EscapedPath()) })
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	for _, tc := range []struct {
		path   string
		status int
		body   string
	}{
		{"/one", 200, "/"}, {"/one/", 200, "/"}, {"/two/a%20b?x=%2F", 200, "/a%20b"},
		{"/", 404, ""}, {"/oneevil", 404, ""}, {"/unknown", 404, ""},
		{"/one/../two", 400, ""}, {"/one/%2e%2e/two", 400, ""}, {"/one//x", 400, ""},
		{"/one%2Fx", 400, ""}, {"/one/x%5cy", 400, ""}, {"/%6fne/x", 400, ""},
		{"/one/%00", 400, ""}, {"/one?x=%zz", 400, ""}, {"/one?x=1;y=2", 400, ""},
	} {
		t.Run(tc.path, func(t *testing.T) {
			res, err := p.Client().Get(p.URL + tc.path)
			if err != nil {
				t.Fatal(err)
			}
			body := readBody(t, res)
			if res.StatusCode != tc.status || (tc.status == 200 && body != tc.body) {
				t.Fatalf("status/body = %d %q", res.StatusCode, body)
			}
		})
	}
	for _, method := range []string{"TRACE", "CONNECT", "CUSTOM"} {
		req, _ := http.NewRequest(method, p.URL+"/one", nil)
		res, err := p.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		readBody(t, res)
		if res.StatusCode != 405 || res.Header.Get("Allow") == "" {
			t.Fatalf("method %s: %d", method, res.StatusCode)
		}
	}
}

func TestRejectsUpgradeAndAbsoluteTarget(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { t.Error("rejected request reached upstream") })
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	req, _ := http.NewRequest("GET", p.URL+"/one", nil)
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "websocket")
	res, err := p.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != 501 {
		t.Fatalf("upgrade status = %d", res.StatusCode)
	}
	conn, err := net.DialTimeout("tcp", p.Listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(3 * time.Second))
	io.WriteString(conn, "GET http://evil.invalid/one HTTP/1.1\r\nHost: public.example\r\nConnection: close\r\n\r\n")
	data, err := io.ReadAll(conn)
	if err != nil || !strings.HasPrefix(string(data), "HTTP/1.1 400") {
		t.Fatalf("absolute target: %q, %v", data, err)
	}
}

func TestRejectsInvalidOperatorTargets(t *testing.T) {
	for _, raw := range []string{"", "/relative", "file:///tmp/a", "ftp://host", "http://user:secret@host", "http://host/base", "http://host?secret=x", "http://host?", "http://host#fragment", "http://host\\evil", "http://host:invalid", "http://host:99999", "http://host:0", "http://host:"} {
		_, err := gateway.New([2]string{raw, "http://127.0.0.1:9002"}, gateway.Options{})
		if err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatalf("target %q: %v", raw, err)
		}
	}
}

func TestRequestTrailersExplicitlyUnsupported(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) { t.Error("request trailers reached upstream") })
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	req, _ := http.NewRequest("POST", p.URL+"/one", strings.NewReader("body"))
	req.ContentLength = -1
	req.Trailer = http.Header{"X-Checksum": {"complete"}}
	res, err := p.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	readBody(t, res)
	if res.StatusCode != 501 {
		t.Fatalf("request trailer status = %d", res.StatusCode)
	}
}
