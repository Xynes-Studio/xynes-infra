package idle

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// LeaseSource can be local or a narrowly authenticated private broker client.
// The public proxy never needs Docker daemon access or container identifiers.
type LeaseSource interface {
	Acquire(context.Context, string) (func(), error)
	Snapshot(string) (Status, error)
	Invalidate(string)
}

type ProxyPolicy struct {
	Group             string   `json:"group"`
	Target            string   `json:"target"`
	Hosts             []string `json:"hosts"`
	MaxBodyBytes      int64    `json:"max_body_bytes"`
	MaxQueuedRequests int      `json:"max_queued_requests"`
	TrustedIngress    []string `json:"trusted_ingress"`
	DynamicTarget     bool     `json:"dynamic_target,omitempty"`
}

type endpointContextKey struct{}

type Proxy struct {
	leases       LeaseSource
	policy       ProxyPolicy
	hosts        map[string]bool
	slots        chan struct{}
	upstream     *httputil.ReverseProxy
	trustedPeers map[netip.Addr]bool
}

var hostPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]$`)

func NewProxy(leases LeaseSource, policy ProxyPolicy) (*Proxy, error) {
	if leases == nil || policy.MaxBodyBytes < 1 || policy.MaxBodyBytes > 64*1024*1024 || policy.MaxQueuedRequests < 1 || policy.MaxQueuedRequests > 256 || len(policy.Hosts) < 1 || len(policy.Hosts) > 8 {
		return nil, ErrDenied
	}
	if policy.MaxBodyBytes*int64(policy.MaxQueuedRequests) > 64*1024*1024 {
		return nil, ErrDenied
	}
	if _, err := leases.Snapshot(policy.Group); err != nil {
		return nil, ErrDenied
	}
	target, err := url.Parse(policy.Target)
	if err != nil || (target.Scheme != "http" && target.Scheme != "https") || target.Hostname() == "" || target.User != nil || target.RawQuery != "" || target.Fragment != "" || (target.Path != "" && target.Path != "/") {
		return nil, ErrDenied
	}
	h := &Proxy{leases: leases, policy: policy, hosts: make(map[string]bool), slots: make(chan struct{}, policy.MaxQueuedRequests), trustedPeers: make(map[netip.Addr]bool)}
	if len(policy.TrustedIngress) > 16 {
		return nil, ErrDenied
	}
	for _, value := range policy.TrustedIngress {
		prefix, err := netip.ParsePrefix(value)
		if err != nil || prefix.Bits() != prefix.Addr().BitLen() {
			return nil, ErrDenied
		}
		h.trustedPeers[prefix.Addr().Unmap()] = true
	}
	for _, host := range policy.Hosts {
		if !hostPattern.MatchString(host) || strings.Contains(host, "..") || h.hosts[host] {
			return nil, ErrDenied
		}
		h.hosts[host] = true
	}
	reverse := httputil.NewSingleHostReverseProxy(target)
	director := reverse.Director
	reverse.Director = func(request *http.Request) {
		originalHost := request.Host
		director(request)
		if endpoint, ok := request.Context().Value(endpointContextKey{}).(*url.URL); ok {
			request.URL.Scheme = endpoint.Scheme
			request.URL.Host = endpoint.Host
		}
		request.Host = originalHost
		request.Header.Del("Forwarded")
		peer, peerErr := netip.ParseAddrPort(request.RemoteAddr)
		chain := request.Header.Get("X-Forwarded-For")
		validChain := len(chain) > 0 && len(chain) <= 1024
		parts := strings.Split(chain, ",")
		if len(parts) > 16 {
			validChain = false
		}
		for _, part := range parts {
			if _, err := netip.ParseAddr(strings.TrimSpace(part)); err != nil {
				validChain = false
			}
		}
		if peerErr != nil || !h.trustedPeers[peer.Addr().Unmap()] || !validChain {
			request.Header.Del("X-Forwarded-For")
		}
		request.Header.Set("X-Forwarded-Host", originalHost)
		request.Header.Set("X-Forwarded-Proto", "https")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	// Go may replay an empty write carrying an idempotency header on a reused
	// connection. Each forwarded request gets one HTTP/1 connection; preserve
	// the caller's headers without allowing transport-level write replay.
	transport.DisableKeepAlives = true
	transport.ForceAttemptHTTP2 = false
	transport.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	if transport.TLSClientConfig == nil {
		transport.TLSClientConfig = &tls.Config{}
	}
	// Clone can carry the default transport's h2 ALPN advertisement. Keep TLS
	// trust settings intact while negotiating the sole enabled protocol.
	transport.TLSClientConfig.NextProtos = []string{"http/1.1"}
	transport.ResponseHeaderTimeout = 45 * time.Second
	transport.MaxConnsPerHost = policy.MaxQueuedRequests
	reverse.Transport = transport
	reverse.FlushInterval = -1
	reverse.ErrorLog = log.New(io.Discard, "", 0)
	reverse.ErrorHandler = func(writer http.ResponseWriter, _ *http.Request, _ error) {
		leases.Invalidate(policy.Group)
		unavailable(writer)
	}
	h.upstream = reverse
	return h, nil
}

func unavailable(writer http.ResponseWriter) {
	writer.Header().Set("Content-Type", "application/json")
	writer.Header().Set("Retry-After", "5")
	writer.WriteHeader(http.StatusServiceUnavailable)
	_, _ = writer.Write([]byte(`{"error":"workload_unavailable"}`))
}

func (h *Proxy) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	host := strings.ToLower(strings.TrimSuffix(request.Host, ":443"))
	if !h.hosts[host] {
		http.NotFound(writer, request)
		return
	}
	upgrade := request.Method == http.MethodConnect || request.Header.Get("Upgrade") != ""
	for _, value := range request.Header.Values("Connection") {
		for _, part := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(part), "upgrade") {
				upgrade = true
			}
		}
	}
	// The runtime does not yet track hijacked connections across shutdown.
	// Refuse them before waking rather than silently bypassing work drain.
	if upgrade {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusNotImplemented)
		_, _ = writer.Write([]byte(`{"error":"upgrade_not_supported"}`))
		return
	}
	if request.URL.Path == "/health" || request.URL.Path == "/api/health" || request.URL.Path == "/metrics" {
		if request.Method != "GET" && request.Method != "HEAD" {
			writer.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		status, err := h.leases.Snapshot(h.policy.Group)
		if err != nil {
			unavailable(writer)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		// Passive probes deliberately describe ingress only. CI/monitoring must
		// inspect real provider readiness or explicitly warm and smoke an app.
		_ = json.NewEncoder(writer).Encode(struct {
			Scope string `json:"scope"`
			State Status `json:"state"`
		}{Scope: "ingress", State: status})
		return
	}
	select {
	case h.slots <- struct{}{}:
		defer func() { <-h.slots }()
	default:
		unavailable(writer)
		return
	}
	if request.ContentLength > h.policy.MaxBodyBytes {
		writer.WriteHeader(http.StatusRequestEntityTooLarge)
		return
	}
	var body []byte
	if request.Body != nil {
		var err error
		body, err = io.ReadAll(http.MaxBytesReader(writer, request.Body, h.policy.MaxBodyBytes))
		if err != nil {
			var limit *http.MaxBytesError
			if errors.As(err, &limit) {
				writer.WriteHeader(http.StatusRequestEntityTooLarge)
			} else {
				writer.WriteHeader(http.StatusBadRequest)
			}
			return
		}
		_ = request.Body.Close()
	}
	release, err := h.leases.Acquire(request.Context(), h.policy.Group)
	if err != nil {
		unavailable(writer)
		return
	}
	defer release()

	ctx := request.Context()
	if h.policy.DynamicTarget {
		resolver, ok := h.leases.(interface {
			Endpoint(context.Context, string) (string, error)
		})
		if !ok {
			unavailable(writer)
			return
		}
		endpoint, err := resolver.Endpoint(ctx, h.policy.Group)
		target, parseErr := url.Parse(endpoint)
		expected, _ := url.Parse(h.policy.Target)
		valid := err == nil && parseErr == nil && target.Scheme == "http" && target.User == nil && target.Path == "" && target.RawQuery == "" && target.Fragment == "" && target.Port() == expected.Port()
		if valid {
			address, e := netip.ParseAddr(target.Hostname())
			valid = e == nil && address.Is4() && address.IsPrivate()
		}
		if !valid {
			h.leases.Invalidate(h.policy.Group)
			unavailable(writer)
			return
		}
		ctx = context.WithValue(ctx, endpointContextKey{}, target)
	}
	forward := request.Clone(ctx)
	forward.Body = io.NopCloser(bytes.NewReader(body))
	forward.GetBody = nil // Dispatched non-idempotent bodies must never be replayed.
	forward.ContentLength = int64(len(body))
	forward.TransferEncoding = nil
	h.upstream.ServeHTTP(writer, forward)
}
