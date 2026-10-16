package idle

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRuntimeIngressDeniesForeignPeersBeforeWake(t *testing.T) {
	calls := 0
	h := ingressGuard(validProxyConfig().Policy, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(204) }))
	for _, test := range []struct {
		peer, path string
		code       int
	}{
		{"172.20.0.2:2300", "/", 204},
		{"172.20.0.3:2300", "/", 403},
		{"127.0.0.1:2300", "/", 403},
		{"127.0.0.1:2300", "/health", 204},
		{"172.20.0.3:2300", "/health", 403},
	} {
		r := httptest.NewRequest("GET", "http://auth.qa.xynes.com"+test.path, nil)
		r.RemoteAddr = test.peer
		r.Header.Set("X-Forwarded-For", "172.20.0.2")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != test.code {
			t.Fatal("untrusted ingress admission changed")
		}
	}
	if calls != 2 {
		t.Fatal("foreign peer reached request handler")
	}
}
