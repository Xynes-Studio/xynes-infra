package idle

import "net/http"

type peerUIDKey struct{}

func peerGuard(uid int, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		actual, ok := r.Context().Value(peerUIDKey{}).(int)
		if !ok || actual != uid {
			w.WriteHeader(403)
			return
		}
		next.ServeHTTP(w, r)
	})
}
