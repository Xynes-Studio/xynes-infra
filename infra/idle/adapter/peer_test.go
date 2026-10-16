package idle

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestPrivatePeerIdentityIsRequiredBeforeTokenOrLifecycle(t *testing.T) {
	calls := 0
	h := peerGuard(12002, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(204) }))
	for _, uid := range []int{-1, 0, 12003, 12004, 12002} {
		r := httptest.NewRequest("POST", "http://control/start", nil)
		if uid >= 0 {
			r = r.WithContext(context.WithValue(r.Context(), peerUIDKey{}, uid))
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if uid == 12002 && w.Code != 204 {
			t.Fatal("approved Unix peer rejected")
		}
		if uid != 12002 && w.Code != 403 {
			t.Fatal("foreign or absent peer reached private control")
		}
	}
	if calls != 1 {
		t.Fatal("unauthorized peer reached lifecycle handler")
	}
}
