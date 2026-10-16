package idle

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestBrokerEndpointFollowsOnlyApprovedCIDAndStageNetwork(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	d.info.Running = true
	network := strings.Repeat("e", 64)
	d.info.Networks = map[string]NetworkEndpoint{"xynes_qa_net": {NetworkID: network, IPAddress: "172.19.0.4"}}
	target := approvedTarget()
	target.NetworkID = network
	b, err := NewBroker(d, "qa", map[string]Target{"qa-auth": target})
	if err != nil {
		t.Fatal(err)
	}
	endpoint, err := b.Endpoint(context.Background(), "qa-auth")
	if err != nil || endpoint != "http://172.19.0.4:3100" {
		t.Fatal("approved endpoint refused")
	}
	d.info.Networks["xynes_qa_net"] = NetworkEndpoint{NetworkID: network, IPAddress: "172.19.0.9"}
	endpoint, err = b.Endpoint(context.Background(), "qa-auth")
	if err != nil || endpoint != "http://172.19.0.9:3100" {
		t.Fatal("woken approved CID's new address was not followed")
	}
	for _, mutation := range []string{"network", "public", "cid", "image", "stopped"} {
		saved := d.info
		saveNetworks := d.info.Networks
		d.info.Networks = map[string]NetworkEndpoint{"xynes_qa_net": {NetworkID: network, IPAddress: "172.19.0.9"}}
		switch mutation {
		case "network":
			d.info.Networks["xynes_qa_net"] = NetworkEndpoint{NetworkID: strings.Repeat("f", 64), IPAddress: "172.19.0.9"}
		case "public":
			d.info.Networks["xynes_qa_net"] = NetworkEndpoint{NetworkID: network, IPAddress: "8.8.8.8"}
		case "cid":
			d.info.ID = strings.Repeat("f", 64)
		case "image":
			d.info.Image = "foreign"
		case "stopped":
			d.info.Running = false
		}
		if _, err := b.Endpoint(context.Background(), "qa-auth"); err == nil {
			t.Fatal("unsafe endpoint accepted:", mutation)
		}
		d.info = saved
		d.info.Networks = saveNetworks
	}
	if len(d.starts) != 0 || len(d.stops) != 0 {
		t.Fatal("endpoint lookup changed lifecycle")
	}
}

type endpointProvider struct {
	fixtureProvider
	url     string
	lookups atomic.Int32
}

func (p *endpointProvider) Endpoint(context.Context, string) (string, error) {
	if p.starts.Load() == 0 {
		return "", ErrDenied
	}
	p.lookups.Add(1)
	return p.url, nil
}
func TestDynamicProxyNeverForwardsWriteToForeignOldEndpoint(t *testing.T) {
	var old, newCalls atomic.Int32
	oldServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { old.Add(1); w.WriteHeader(418) }))
	defer oldServer.Close()
	newServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != "POST" || r.URL.RawQuery != "state=original" || string(body) != "original" || r.Header.Get("Authorization") != "Bearer unchanged" {
			t.Error("original write changed")
		}
		newCalls.Add(1)
		w.WriteHeader(201)
	}))
	defer newServer.Close()
	provider := &endpointProvider{url: "http://172.19.0.9:3100"}
	manager, err := NewManager(provider, map[string]Policy{"qa-auth": {IdleFor: 1000000000, WakeTimeout: 1000000000, MaxInFlight: 4}}, func() time.Time { return time.Now() })
	if err != nil {
		t.Fatal(err)
	}
	defer manager.Close()
	proxy, err := NewProxy(manager, ProxyPolicy{Group: "qa-auth", Target: "http://172.19.0.4:3100", Hosts: []string{"auth.qa.xynes.com"}, MaxBodyBytes: 1024, MaxQueuedRequests: 4, DynamicTarget: true})
	if err != nil {
		t.Fatal(err)
	}
	transport := proxy.upstream.Transport.(*http.Transport)
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		target := strings.TrimPrefix(oldServer.URL, "http://")
		if address == "172.19.0.9:3100" {
			target = strings.TrimPrefix(newServer.URL, "http://")
		}
		return (&net.Dialer{}).DialContext(ctx, network, target)
	}
	request := httptest.NewRequest("POST", "http://auth.qa.xynes.com/create?state=original", strings.NewReader("original"))
	request.Header.Set("Authorization", "Bearer unchanged")
	result := httptest.NewRecorder()
	proxy.ServeHTTP(result, request)
	if result.Code != 201 || newCalls.Load() != 1 || old.Load() != 0 || provider.lookups.Load() != 1 {
		t.Fatal("write was lost, replayed or leaked to stale IP")
	}
}

func TestAuthenticatedEndpointClientRoundTripAndClosedResponse(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	d.info.Running = true
	network := strings.Repeat("e", 64)
	d.info.Networks = map[string]NetworkEndpoint{"xynes_qa_net": {NetworkID: network, IPAddress: "172.19.0.9"}}
	target := approvedTarget()
	target.NetworkID = network
	broker, err := NewBroker(d, "qa", map[string]Target{"qa-auth": target})
	if err != nil {
		t.Fatal(err)
	}
	handler, err := NewControlHandler(broker, strings.Repeat("f", 64))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	client := newControlHTTPClient(server.URL, strings.Repeat("f", 64), server.Client())
	value, err := client.Endpoint(context.Background(), "qa-auth")
	if err != nil || value != "http://172.19.0.9:3100" || len(d.starts) != 0 {
		t.Fatal("authenticated passive endpoint refused")
	}
	if _, err = client.Endpoint(context.Background(), "prod-auth"); err != ErrDenied {
		t.Fatal("foreign endpoint allowed")
	}
	d.info.Running = false
	if _, err = client.Endpoint(context.Background(), "qa-auth"); err == nil {
		t.Fatal("stopped endpoint returned")
	}
	for _, row := range []struct {
		status int
		body   string
	}{{401, ""}, {403, ""}, {500, ""}, {200, `{}`}, {200, `{"endpoint":"http://172.19.0.9:3100","foreign":true}`}, {200, `{"endpoint":"http://172.19.0.9:3100"} {}`}, {200, strings.Repeat(" ", 1025)}} {
		broken := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(row.status); w.Write([]byte(row.body)) }))
		_, err = newControlHTTPClient(broken.URL, strings.Repeat("f", 64), broken.Client()).Endpoint(context.Background(), "qa-auth")
		broken.Close()
		if err == nil {
			t.Fatal("malformed/private endpoint response accepted")
		}
	}
}
