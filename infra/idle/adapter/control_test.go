package idle

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPrivateControlRequiresStageTokenBeforeDockerAccess(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	token := strings.Repeat("f", 64)
	h, err := NewControlHandler(broker(t, d), token)
	if err != nil {
		t.Fatal(err)
	}
	for _, auth := range []string{"", "Bearer user-jwt", "Bearer " + strings.Repeat("a", 64)} {
		r := httptest.NewRequest("POST", "http://control/start", strings.NewReader(`{"group":"qa-auth"}`))
		r.Header.Set("Authorization", auth)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 401 || len(d.inspects) != 0 || strings.Contains(w.Body.String(), token) {
			t.Fatal("private control trust boundary failed")
		}
	}
}

func TestPrivateControlRejectsForeignGroupsAndArbitraryOperations(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	token := strings.Repeat("f", 64)
	h, err := NewControlHandler(broker(t, d), token)
	if err != nil {
		t.Fatal(err)
	}
	for _, body := range []string{`{"group":"prod-auth"}`, `{"group":"qa-auth","container_id":"prod-db"}`, `{"group":"qa-auth"} PRIVATE-MATERIAL`, strings.Repeat("x", 2048)} {
		r := httptest.NewRequest("POST", "http://control/start", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code < 400 || len(d.inspects) != 0 || strings.Contains(w.Body.String(), "PRIVATE") {
			t.Fatal("unapproved control request reached Docker")
		}
	}
}

func TestAuthenticatedControlClientReadyStartStopRoundTrip(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	token := strings.Repeat("f", 64)
	handler, err := NewControlHandler(broker(t, d), token)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	client := newControlHTTPClient(server.URL, token, server.Client())
	if err := client.EnsureReady(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
	if err := client.Stop(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
	if len(d.starts) != 1 || len(d.stops) != 1 {
		t.Fatal("control operations changed")
	}
	if err := client.Stop(context.Background(), "prod-auth"); err != ErrDenied {
		t.Fatal("foreign group accepted")
	}
}

func TestControlDialFailureIsKnownNotDispatched(t *testing.T) {
	client := newControlHTTPClient("http://127.0.0.1:1", strings.Repeat("f", 64), http.DefaultClient)
	if err := client.Stop(context.Background(), "qa-auth"); err != ErrControlUnavailable {
		t.Fatal("undispatched control failure not distinguished")
	}
}

func TestPrivateControlHealthIsAuthenticatedAndPassive(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	token := strings.Repeat("f", 64)
	handler, err := NewControlHandler(broker(t, d), token)
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest("GET", "http://control/ready", nil)
	r.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 200 || len(d.inspects) != 0 {
		t.Fatal("control probe touched Docker")
	}
	body, _ := io.ReadAll(w.Result().Body)
	if strings.Contains(string(body), token) {
		t.Fatal("private token leaked")
	}
}
