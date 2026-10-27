package bench

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"example.com/resilient-api-gateway/internal/gateway"
)

func BenchmarkGatewayRequest(b *testing.B) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { io.WriteString(w, "x") }))
	defer upstream.Close()
	g, err := gateway.New([2]string{upstream.URL, upstream.URL}, gateway.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err != nil {
		b.Fatal(err)
	}
	defer g.CloseIdleConnections()
	server := httptest.NewServer(g)
	defer server.Close()
	client := server.Client()
	client.Get(server.URL + "/one/warmup")
	b.ReportAllocs()
	b.SetBytes(1)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		res, err := client.Get(server.URL + "/one/bench")
		if err != nil {
			b.Fatal(err)
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
	}
}
