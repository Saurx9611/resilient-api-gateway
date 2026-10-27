package gateway_test

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"example.com/resilient-api-gateway/internal/gateway"
)

func TestStreamingUpload(t *testing.T) {
	firstReceived := make(chan struct{})
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		first := make([]byte, 5)
		if _, err := io.ReadFull(r.Body, first); err != nil {
			return
		}
		close(firstReceived)
		rest, err := io.ReadAll(r.Body)
		if err != nil {
			return
		}
		io.WriteString(w, string(first)+string(rest))
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	req, _ := http.NewRequest("POST", p.URL+"/one/upload", reader)
	done := make(chan error, 1)
	go func() {
		res, err := p.Client().Do(req)
		if err == nil {
			defer res.Body.Close()
			var data []byte
			data, err = io.ReadAll(res.Body)
			if err == nil && string(data) != "first-last" {
				err = io.ErrUnexpectedEOF
			}
		}
		done <- err
	}()
	if _, err := io.WriteString(writer, "first"); err != nil {
		t.Fatal(err)
	}
	// No EOF yet: upstream must see the first bytes before the producer finishes.
	await(t, firstReceived)
	if _, err := io.WriteString(writer, "-last"); err != nil {
		t.Fatal(err)
	}
	writer.Close()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestStreamingResponse(t *testing.T) {
	release, unblock := context.WithCancel(context.Background())
	defer unblock()
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "first")
		w.(http.Flusher).Flush()
		select {
		case <-release.Done():
		case <-r.Context().Done():
			return
		}
		io.WriteString(w, "-last")
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	res, err := p.Client().Get(p.URL + "/one/stream")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	first := make([]byte, 5)
	if _, err := io.ReadFull(res.Body, first); err != nil || string(first) != "first" {
		t.Fatalf("first chunk = %q, %v", first, err)
	}
	// First bytes arrived while the upstream is still waiting to finish.
	unblock()
	if rest := readBody(t, res); rest != "-last" {
		t.Fatalf("rest = %q", rest)
	}
}

func TestEarlyResponseWithoutDrainingUpload(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		http.NewResponseController(w).EnableFullDuplex()
		w.Header().Set("Connection", "close")
		w.Header().Set("Content-Length", "0")
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		w.(http.Flusher).Flush()
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	conn, err := net.DialTimeout("tcp", p.Listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(3 * time.Second))
	// Intentionally send no body. The gateway must forward the early response.
	io.WriteString(conn, "POST /one/upload HTTP/1.1\r\nHost: public.example\r\nContent-Length: 1000000\r\n\r\n")
	res, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 413 {
		t.Fatalf("early status = %d", res.StatusCode)
	}
}

type unreadBody struct{ read bool }

func (b *unreadBody) Read([]byte) (int, error) { b.read = true; return 0, io.EOF }
func (*unreadBody) Close() error               { return nil }

func TestRejectsBeforeApplicationBodyRead(t *testing.T) {
	g, err := gateway.New([2]string{"http://127.0.0.1:9001", "http://127.0.0.1:9002"}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	for _, tc := range []struct {
		method, path string
		status       int
	}{{"POST", "/unknown", 404}, {"TRACE", "/one", 405}, {"POST", "/one/%2e%2e/x", 400}} {
		req := httptest.NewRequest(tc.method, tc.path, nil)
		body := new(unreadBody)
		req.Body, req.ContentLength = body, 1000000
		rw := httptest.NewRecorder()
		g.ServeHTTP(rw, req)
		if body.read || rw.Code != tc.status || rw.Header().Get("Connection") != "close" {
			t.Fatalf("rejection = %d; read = %v", rw.Code, body.read)
		}
	}
}

func TestClientCancellationReachesUpstream(t *testing.T) {
	started, canceled := make(chan struct{}), make(chan struct{})
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-r.Context().Done()
		close(canceled)
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", p.URL+"/one", nil)
	done := make(chan error, 1)
	go func() {
		res, err := p.Client().Do(req)
		if res != nil {
			res.Body.Close()
		}
		done <- err
	}()
	await(t, started)
	cancel()
	if err := <-done; err == nil {
		t.Fatal("canceled client succeeded")
	}
	await(t, canceled)
}

func TestStreamDisconnectAbortsResponse(t *testing.T) {
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		conn, buf, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer conn.Close()
		buf.WriteString("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
		buf.Flush()
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{})
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if res.StatusCode != 200 || err == nil || !strings.HasPrefix(string(data), "short") {
		t.Fatalf("truncated response = %d %q %v", res.StatusCode, data, err)
	}
}

func TestResponseDeadlineAfterHeadersAbortsStream(t *testing.T) {
	canceled := make(chan struct{})
	u := upstream(t, func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "first")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(canceled)
	})
	p := startProxy(t, [2]string{u.URL, u.URL}, gateway.Options{RequestTimeout: 250 * time.Millisecond})
	res, err := p.Client().Get(p.URL + "/one")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	if res.StatusCode != 200 || err == nil || string(body) != "first" {
		t.Fatalf("expired stream = %d %q %v", res.StatusCode, body, err)
	}
	await(t, canceled)
}

func TestRejectedUploadRespondsWithoutWaitingForBody(t *testing.T) {
	g, err := gateway.New([2]string{"http://127.0.0.1:9001", "http://127.0.0.1:9002"}, gateway.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer g.CloseIdleConnections()
	p := httptest.NewServer(g)
	defer p.Close()
	conn, err := net.DialTimeout("tcp", p.Listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(3 * time.Second))
	io.WriteString(conn, "POST /unknown HTTP/1.1\r\nHost: public.example\r\nContent-Length: 1000000\r\n\r\n")
	res, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 404 {
		t.Fatalf("rejection status = %d", res.StatusCode)
	}
}
