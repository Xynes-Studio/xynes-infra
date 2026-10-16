package idle

import (
	"context"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPrivateWarmEndpointRejectsUntrustedPeersAndMalformedRequests(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	token := strings.Repeat("f", 64)
	h, err := NewWarmHandler(m, "qa-auth", token)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		peer        int
		token, body string
		code        int
	}{
		{12002, token, `{"group":"qa-auth","owner":"deploy","ttl_seconds":3600}`, 403},
		{0, "Bearer user-jwt", `{}`, 401},
		{0, token, `{"group":"qa-auth","owner":"deploy","ttl_seconds":3600,"container_id":"prod"}`, 400},
		{0, token, `{"group":"prod-auth","owner":"deploy","ttl_seconds":3600}`, 403},
		{0, token, `{"group":"qa-auth","owner":"deploy","ttl_seconds":86401}`, 403},
	} {
		r := httptest.NewRequest("POST", "http://lease/warm", strings.NewReader(test.body))
		r.Header.Set("Authorization", "Bearer "+test.token)
		r = r.WithContext(context.WithValue(r.Context(), peerUIDKey{}, test.peer))
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != test.code || p.starts.Load() != 0 || strings.Contains(w.Body.String(), token) {
			t.Fatal("untrusted warm operation reached provider or leaked credential")
		}
	}
}

func TestPrivateWarmAndReleaseUseDurableOwnedState(t *testing.T) {
	c := &fixtureClock{value: time.Unix(1000, 0)}
	p := &fixtureProvider{}
	m := persistentFixture(t, filepath.Join(privateLeaseDir(t), "warm.json"), p, c)
	token := strings.Repeat("f", 64)
	h, err := NewWarmHandler(m, "qa-auth", token)
	if err != nil {
		t.Fatal(err)
	}
	call := func(path, body string) int {
		r := httptest.NewRequest("POST", "http://lease"+path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		r = r.WithContext(context.WithValue(r.Context(), peerUIDKey{}, 0))
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w.Code
	}
	if call("/warm", `{"group":"qa-auth","owner":"deploy-release","ttl_seconds":3600}`) != 204 {
		t.Fatal("owned hold not accepted")
	}
	c.advance(time.Minute)
	m.Tick(context.Background())
	if p.stops.Load() != 0 {
		t.Fatal("active deployment stopped")
	}
	if call("/release", `{"group":"qa-auth","owner":"someone-else"}`) != 403 {
		t.Fatal("foreign release accepted")
	}
	if call("/release", `{"group":"qa-auth","owner":"deploy-release"}`) != 204 {
		t.Fatal("owned release failed")
	}
	c.advance(time.Second)
	m.Tick(context.Background())
	if p.stops.Load() != 1 {
		t.Fatal("released group never idles")
	}
}
