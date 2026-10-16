package idle

import (
	"net/http"
	"net/netip"
)

func ingressGuard(policy ProxyPolicy, next http.Handler) http.Handler {
	peers := make(map[netip.Addr]bool)
	for _, value := range policy.TrustedIngress {
		prefix, err := netip.ParsePrefix(value)
		if err == nil && prefix.Bits() == prefix.Addr().BitLen() {
			peers[prefix.Addr().Unmap()] = true
		}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		peer, err := netip.ParseAddrPort(r.RemoteAddr)
		probe := r.URL.Path == "/health" || r.URL.Path == "/api/health" || r.URL.Path == "/metrics"
		if err != nil || (!peers[peer.Addr().Unmap()] && !(peer.Addr().IsLoopback() && probe)) {
			w.WriteHeader(403)
			return
		}
		next.ServeHTTP(w, r)
	})
}
