package idle

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func proxyFor(t *testing.T, m *Manager, upstream string, limit int64) http.Handler {
	t.Helper()
	h, err := NewProxy(m, ProxyPolicy{Group: "qa-auth", Target: upstream, Hosts: []string{"qa-auth.example.invalid"}, MaxBodyBytes: limit, MaxQueuedRequests: 8})
	if err != nil {
		t.Fatal(err)
	}
	return h
}

func appRequest(method, path, body string) *http.Request {
	r := httptest.NewRequest(method, "http://qa-auth.example.invalid"+path, strings.NewReader(body))
	r.Header.Set("Authorization", "Bearer synthetic-fixture")
	return r
}

func TestProxyPreservesFirstWriteAndUpstreamStatus(t *testing.T) {
	var delivered atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		delivered.Add(1)
		body, _ := io.ReadAll(r.Body)
		if r.Method != "POST" || r.URL.RequestURI() != "/callback?state=fixture" || string(body) != "unchanged" || r.Header.Get("Authorization") != "Bearer synthetic-fixture" || r.Host != "qa-auth.example.invalid" {
			t.Error("request changed")
		}
		w.Header().Set("X-Fixture", "retained")
		w.WriteHeader(201)
		_, _ = w.Write([]byte("created"))
	}))
	defer upstream.Close()
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	response := httptest.NewRecorder()
	proxyFor(t, m, upstream.URL, 1024).ServeHTTP(response, appRequest("POST", "/callback?state=fixture", "unchanged"))
	if response.Code != 201 || response.Header().Get("X-Fixture") != "retained" || response.Body.String() != "created" {
		t.Fatal("upstream status/body/header changed")
	}
	if delivered.Load() != 1 || p.starts.Load() != 1 {
		t.Fatal("first write replayed or missed")
	}
}

func TestProbeAndUnknownHostNeverWake(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	h := proxyFor(t, m, "http://127.0.0.1:1", 1024)
	for _, path := range []string{"/health", "/api/health", "/metrics"} {
		response := httptest.NewRecorder()
		h.ServeHTTP(response, appRequest("GET", path, ""))
		if response.Code != 200 || !strings.Contains(response.Body.String(), `"scope":"ingress"`) {
			t.Fatal("probe incorrectly claims app readiness")
		}
	}
	r := appRequest("GET", "/", " ")
	r.Host = "prod-auth.example.invalid"
	response := httptest.NewRecorder()
	h.ServeHTTP(response, r)
	if response.Code != 404 || p.starts.Load() != 0 {
		t.Fatal("foreign host caused wake")
	}
}

func TestUnsupportedUpgradeAndTunnelNeverWakeOrForward(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	h := proxyFor(t, m, "http://127.0.0.1:1", 1024)
	for _, kind := range []string{"websocket", "connection-only", "connect"} {
		r := appRequest("GET", "/socket", "")
		switch kind {
		case "websocket":
			r.Header.Set("Connection", "keep-alive, UpGrAdE")
			r.Header.Set("Upgrade", "websocket")
		case "connection-only":
			r.Header.Set("Connection", "Upgrade")
		case "connect":
			r.Method = "CONNECT"
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 501 || w.Header().Get("Content-Type") != "application/json" || p.starts.Load() != 0 {
			t.Fatal("unsupported hijacked work admitted")
		}
	}
}

func TestOversizedKnownAndChunkedBodyRefusedBeforeWake(t *testing.T) {
	for _, chunked := range []bool{false, true} {
		p := &fixtureProvider{}
		m, _ := setup(t, p, 8)
		r := appRequest("POST", "/", "too-large")
		if chunked {
			r.ContentLength = -1
		}
		response := httptest.NewRecorder()
		proxyFor(t, m, "http://127.0.0.1:1", 4).ServeHTTP(response, r)
		if response.Code != 413 || p.starts.Load() != 0 {
			t.Fatal("oversized body admitted")
		}
	}
}

func TestProxyRejectsAggregateBodyAdmissionBeyondMemoryBudget(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	_, err := NewProxy(m, ProxyPolicy{Group: "qa-auth", Target: "http://127.0.0.1:1", Hosts: []string{"qa-auth.example.invalid"}, MaxBodyBytes: 64 * 1024 * 1024, MaxQueuedRequests: 256})
	if err != ErrDenied {
		t.Fatal("request admission permits unbounded host-sized body allocation")
	}
}

func TestReadinessAndTransportFailureHaveSafe503(t *testing.T) {
	for _, readinessFail := range []bool{false, true} {
		p := &fixtureProvider{fail: readinessFail}
		m, _ := setup(t, p, 8)
		response := httptest.NewRecorder()
		proxyFor(t, m, "http://127.0.0.1:1", 1024).ServeHTTP(response, appRequest("GET", "/", ""))
		if response.Code != 503 || response.Header().Get("Retry-After") == "" || strings.Contains(response.Body.String(), "DO-NOT-EXPOSE") {
			t.Fatal("unsafe or misleading failure response")
		}
	}
}

func TestProxyHoldsLeaseThroughCompleteStream(t *testing.T) {
	started := make(chan struct{})
	finish := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: first\n\n"))
		w.(http.Flusher).Flush()
		close(started)
		<-finish
		_, _ = w.Write([]byte("data: last\n\n"))
	}))
	defer upstream.Close()
	p := &fixtureProvider{}
	m, clock := setup(t, p, 8)
	proxy := httptest.NewServer(proxyFor(t, m, upstream.URL, 1024))
	defer proxy.Close()
	r, err := http.NewRequestWithContext(context.Background(), "GET", proxy.URL+"/stream", nil)
	if err != nil {
		t.Fatal(err)
	}
	r.Host = "qa-auth.example.invalid"
	response, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	<-started
	clock.advance(36 * time.Second)
	_ = m.Tick(context.Background())
	if p.stops.Load() != 0 {
		t.Fatal("active stream stopped")
	}
	close(finish)
	body, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != "data: first\n\ndata: last\n\n" {
		t.Fatal("stream incomplete")
	}
	clock.advance(2 * time.Second)
	_ = m.Tick(context.Background())
	if p.stops.Load() != 1 {
		t.Fatal("finished stream did not become idle")
	}
}

func TestProxyDoesNotRetryDispatchedWriteOnDisconnect(t *testing.T) {
	var deliveries atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		deliveries.Add(1)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		_ = connection.Close()
	}))
	defer upstream.Close()
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	response := httptest.NewRecorder()
	proxyFor(t, m, upstream.URL, 1024).ServeHTTP(response, appRequest("POST", "/write", "body"))
	if response.Code != 503 || deliveries.Load() != 1 {
		t.Fatal("dispatched write replayed")
	}
}

func TestTransportFailureRechecksReadinessOnNextRequest(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	h := proxyFor(t, m, "http://127.0.0.1:1", 1024)
	for i := 0; i < 2; i++ {
		response := httptest.NewRecorder()
		h.ServeHTTP(response, appRequest("GET", "/", ""))
		if response.Code != 503 {
			t.Fatal("transport failure not refused")
		}
	}
	if p.starts.Load() != 2 {
		t.Fatal("stale ready state prevented wake/recovery")
	}
}

func TestClientIPChainIsPreservedOnlyForConfiguredIngressPeer(t *testing.T) {
	for _, trusted := range []bool{false, true} {
		var chain string
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			chain = r.Header.Get("X-Forwarded-For")
			w.WriteHeader(204)
		}))
		p := &fixtureProvider{}
		m, _ := setup(t, p, 8)
		policy := ProxyPolicy{Group: "qa-auth", Target: upstream.URL, Hosts: []string{"qa-auth.example.invalid"}, MaxBodyBytes: 1024, MaxQueuedRequests: 8}
		if trusted {
			policy.TrustedIngress = []string{"192.0.2.9/32"}
		}
		h, err := NewProxy(m, policy)
		if err != nil {
			t.Fatal(err)
		}
		r := appRequest("GET", "/", "")
		r.RemoteAddr = "192.0.2.9:1234"
		r.Header.Set("X-Forwarded-For", "203.0.113.7")
		h.ServeHTTP(httptest.NewRecorder(), r)
		upstream.Close()
		if strings.Contains(chain, "203.0.113.7") != trusted {
			t.Fatal("client IP trust boundary violated")
		}
	}
}

func TestProxyNeverReplaysZeroBodyWriteAfterReusedConnectionDisconnect(t *testing.T) {
	for _, header := range []string{"Idempotency-Key", "X-Idempotency-Key"} {
		t.Run(header, func(t *testing.T) {
			var delivered atomic.Int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "GET" {
					w.WriteHeader(200)
					_, _ = w.Write([]byte("warm"))
					return
				}
				delivered.Add(1)
				if r.Header.Get(header) != "fixture-key" {
					t.Error("write header changed")
				}
				connection, _, err := w.(http.Hijacker).Hijack()
				if err != nil {
					t.Error(err)
					return
				}
				_ = connection.Close()
			}))
			defer upstream.Close()
			provider := &fixtureProvider{}
			manager, _ := setup(t, provider, 8)
			handler := proxyFor(t, manager, upstream.URL, 1024)
			warm := httptest.NewRecorder()
			handler.ServeHTTP(warm, appRequest("GET", "/warm", ""))
			if warm.Code != 200 {
				t.Fatal("warm connection setup failed")
			}
			request := appRequest("POST", "/write", "")
			request.Header.Set(header, "fixture-key")
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			if response.Code != 503 || delivered.Load() != 1 {
				t.Fatalf("ambiguous dispatched write replayed: status=%d deliveries=%d", response.Code, delivered.Load())
			}
		})
	}
}

func TestProxyHTTPSPreservesSingleDispatchWhenUpstreamAdvertisesHTTP2(t *testing.T) {
	var deliveries atomic.Int32
	upstream := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor != 1 {
			t.Error("single-dispatch backend transport negotiated HTTP2")
		}
		if r.Header.Get("Idempotency-Key") != "fixture-key" {
			t.Error("application header changed")
		}
		deliveries.Add(1)
		w.WriteHeader(201)
	}))
	upstream.EnableHTTP2 = true
	upstream.StartTLS()
	defer upstream.Close()
	prior := http.DefaultTransport
	seed := prior.(*http.Transport).Clone()
	seed.TLSClientConfig = upstream.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
	seed.ForceAttemptHTTP2 = true
	_ = seed.Clone() // Initialize the ordinary default HTTP2/ALPN configuration.
	http.DefaultTransport = seed
	defer func() { http.DefaultTransport = prior; seed.CloseIdleConnections() }()
	provider := &fixtureProvider{}
	manager, _ := setup(t, provider, 8)
	handler := proxyFor(t, manager, upstream.URL, 1024)
	request := appRequest("POST", "/write", "")
	request.Header.Set("Idempotency-Key", "fixture-key")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != 201 || deliveries.Load() != 1 {
		t.Fatalf("HTTPS forwarding failed: status=%d deliveries=%d", response.Code, deliveries.Load())
	}
}
