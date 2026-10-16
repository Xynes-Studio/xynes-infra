//go:build linux

package idle

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// One isolated internal-network container supplies private host-style addresses.
// The Docker socket below is a synthetic fixture listener, never the host daemon.
func TestLinuxHostNetworkGenerationDrainAndRestart(t *testing.T) {
	if os.Geteuid() != 0 || os.Getenv("XYNES_IDLE_HOST_FIXTURE") != "1" {
		t.Skip("explicit isolated host-network fixture required")
	}
	interfaces, err := net.InterfaceAddrs()
	if err != nil {
		t.Fatal(err)
	}
	private := ""
	for _, address := range interfaces {
		ip, _, e := net.ParseCIDR(address.String())
		if e == nil && ip.To4() != nil && ip.IsPrivate() {
			private = ip.String()
			break
		}
	}
	if private == "" {
		t.Fatal("fixture needs its own private internal bridge")
	}
	alternateIP := net.ParseIP(private).To4()
	alternateIP[3] = 250
	if alternateIP.String() == private {
		alternateIP[3] = 249
	}
	alternate := alternateIP.String()
	if out, err := exec.Command("ip", "address", "add", alternate+"/32", "dev", "eth0").CombinedOutput(); err != nil {
		t.Fatalf("isolated fixture address alias: %s", out)
	}

	for _, dir := range []string{"/run/xynes/idle/qa", "/etc/xynes/idle/qa", "/var/lib/xynes/idle/qa"} {
		if os.MkdirAll(dir, 0750) != nil || os.Chown(dir, 0, 12002) != nil {
			t.Fatal("stage fixture metadata")
		}
	}
	for _, dir := range []string{"/run/xynes", "/run/xynes/idle", "/etc/xynes/idle", "/var/lib/xynes/idle"} {
		if os.Chmod(dir, 0755) != nil {
			t.Fatal("fixture traversal")
		}
	}
	for _, dir := range []string{"/run/xynes/idle/qa/auth", "/var/lib/xynes/idle/qa/auth"} {
		if os.Mkdir(dir, 0700) != nil || os.Chown(dir, 12002, 12002) != nil {
			t.Fatal("owned fixture leaf")
		}
	}
	token := strings.Repeat("f", 64)
	tokenPath := "/etc/xynes/idle/qa/control.token"
	if os.WriteFile(tokenPath, []byte(token), 0440) != nil || os.Chown(tokenPath, 0, 12002) != nil {
		t.Fatal("fixture token")
	}
	p := validProxyConfig()
	p.HostNetwork = true
	p.Policy.DynamicTarget = true
	p.StartDrained = true
	p.Listen = private + ":18081"
	p.Policy.Target = "http://" + private + ":3100"
	p.Policy.TrustedIngress = []string{private + "/32"}
	originalExpiry := time.Now().Add(time.Hour).UnixNano()
	warm := warmDocument{Version: 1, Environment: "qa", Group: "qa-auth", Leases: []warmRecord{{Owner: "deploy-42", Until: originalExpiry}}}
	raw, _ := json.Marshal(warm)
	if os.WriteFile(p.StateFile, raw, 0600) != nil || os.Chown(p.StateFile, 12002, 12002) != nil {
		t.Fatal("fresh durable deployment hold")
	}
	writeProxy := func() {
		t.Helper()
		data, _ := json.Marshal(p)
		path := "/etc/xynes/idle/qa/proxy.json"
		if os.WriteFile(path, data, 0640) != nil || os.Chown(path, 0, 12002) != nil {
			t.Fatal("protected proxy config")
		}
	}
	writeProxy()
	var running atomic.Bool
	var starts, stops, deliveries atomic.Int32
	var cid, image, endpointIP atomic.Value
	endpointIP.Store(private)
	cid.Store(strings.Repeat("a", 64))
	image.Store(approvedTarget().Image)
	daemon, err := net.Listen("unix", "/var/run/docker.sock")
	if err != nil {
		t.Fatal(err)
	}
	docker := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1.49/containers/"+cid.Load().(string)+"/json" && r.URL.Path != "/v1.49/containers/"+cid.Load().(string)+"/start" && r.URL.Path != "/v1.49/containers/"+cid.Load().(string)+"/stop" {
			w.WriteHeader(404)
			return
		}
		switch {
		case strings.HasSuffix(r.URL.Path, "/json"):
			json.NewEncoder(w).Encode(map[string]any{"Id": cid.Load(), "Config": map[string]any{"Image": image.Load(), "Labels": approvedContainer().Labels}, "NetworkSettings": map[string]any{"Networks": map[string]any{"xynes_qa_net": map[string]string{"NetworkID": strings.Repeat("e", 64), "IPAddress": endpointIP.Load().(string)}}}, "State": map[string]any{"Running": running.Load(), "Health": map[string]string{"Status": "healthy"}}})
		case strings.HasSuffix(r.URL.Path, "/start"):
			running.Store(true)
			starts.Add(1)
			w.WriteHeader(204)
		case strings.HasSuffix(r.URL.Path, "/stop"):
			running.Store(false)
			stops.Add(1)
			w.WriteHeader(204)
		}
	})}
	go docker.Serve(daemon)
	defer docker.Close()
	brokerStart := func() (context.CancelFunc, chan error) {
		t.Helper()
		config := validBrokerConfig()
		config.Targets = map[string]Target{"qa-auth": {ContainerID: cid.Load().(string), Image: image.Load().(string), Service: "auth-app", NetworkID: strings.Repeat("e", 64)}}
		ctx, cancel := context.WithCancel(context.Background())
		result := make(chan error, 1)
		go func() { result <- RunBroker(ctx, config) }()
		deadline := time.Now().Add(5 * time.Second)
		for {
			if _, err := os.Lstat(config.ControlSocket); err == nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("broker startup")
			}
			time.Sleep(10 * time.Millisecond)
		}
		return cancel, result
	}
	cancelBroker, brokerResult := brokerStart()
	defer func() { cancelBroker(); <-brokerResult }()
	finishStream := make(chan struct{})
	upstreamListener, err := net.Listen("tcp", private+":3100")
	if err != nil {
		t.Fatal(err)
	}
	var oldEndpointForeign atomic.Bool
	var oldEndpointDeliveries atomic.Int32
	commonHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !running.Load() {
			w.WriteHeader(503)
			return
		}
		if r.Method == "GET" && r.URL.Path == "/stream" {
			w.WriteHeader(200)
			w.Write([]byte("start\n"))
			w.(http.Flusher).Flush()
			select {
			case <-finishStream:
				w.Write([]byte("end\n"))
			case <-r.Context().Done():
			}
			return
		}
		body, _ := io.ReadAll(r.Body)
		if r.Method != "POST" || r.URL.RawQuery != "one=1" || string(body) != "original" || r.Header.Get("Authorization") != "Bearer synthetic" {
			t.Error("cold original request changed")
		}
		deliveries.Add(1)
		w.WriteHeader(201)
		w.Write([]byte("created"))

	})
	upstream := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if oldEndpointForeign.Load() {
			oldEndpointDeliveries.Add(1)
			w.WriteHeader(418)
			return
		}
		commonHandler.ServeHTTP(w, r)
	})}
	go upstream.Serve(upstreamListener)
	defer upstream.Close()
	child := fixtureChild(t, "proxy", "^TestUnixChildProxy$")
	if child.Start() != nil {
		t.Fatal("proxy startup")
	}
	defer func() {
		if child.ProcessState == nil {
			child.Process.Kill()
			child.Wait()
		}
	}()
	client := &http.Client{Timeout: 5 * time.Second}
	url := "http://" + private + ":18081"
	request := func(method, path string) *http.Request {
		r, _ := http.NewRequest(method, url+path, strings.NewReader("original"))
		r.Host = "auth.qa.xynes.com"
		r.Header.Set("Authorization", "Bearer synthetic")
		return r
	}
	waitProxy := func() {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for {
			r := request("GET", "/health")
			response, e := client.Do(r)
			if e == nil {
				response.Body.Close()
				if response.StatusCode == 200 {
					return
				}
			}
			if time.Now().After(deadline) {
				t.Fatal("host proxy did not become passive-ready")
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
	waitProxy()
	if starts.Load() != 0 {
		t.Fatal("startup woke stopped application")
	}
	response, err := client.Do(request("POST", "/create?one=1"))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if response.StatusCode != 503 || !strings.Contains(string(body), "workload_unavailable") || deliveries.Load() != 0 {
		t.Fatal("new generation opened before root validation")
	}
	leaseClient, err := unixHTTPClient(p.leaseSocket())
	if err != nil {
		t.Fatal(err)
	}
	lease := func(path string) int {
		r, _ := http.NewRequest("POST", "http://lease"+path, strings.NewReader(`{"group":"qa-auth","owner":"deploy-42"}`))
		r.Header.Set("Authorization", "Bearer "+token)
		response, e := leaseClient.Do(r)
		if e != nil {
			return 0
		}
		response.Body.Close()
		return response.StatusCode
	}
	if lease("/resume") != 204 {
		t.Fatal("validated fresh resume")
	}
	response, err = client.Do(request("POST", "/create?one=1"))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 201 || deliveries.Load() != 1 || starts.Load() != 1 {
		t.Fatal("real cold host-network write was lost or replayed")
	}
	// A loopback source is not the configured ingress peer for application work.
	foreign := &http.Client{Transport: &http.Transport{DialContext: (&net.Dialer{LocalAddr: &net.TCPAddr{IP: net.ParseIP("127.0.0.1")}}).DialContext}, Timeout: time.Second}
	response, err = foreign.Do(request("POST", "/create?one=1"))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 403 || deliveries.Load() != 1 {
		t.Fatal("foreign host peer admitted")
	}
	stream, err := client.Do(request("GET", "/stream"))
	if err != nil {
		t.Fatal(err)
	}
	started := make([]byte, 6)
	if _, err = io.ReadFull(stream.Body, started); err != nil || string(started) != "start\n" {
		t.Fatal("stream start")
	}
	drained := make(chan int, 1)
	go func() { drained <- lease("/drain") }()
	deadline := time.Now().Add(time.Second)
	for {
		response, err = client.Do(request("GET", "/health"))
		if err != nil {
			t.Fatal(err)
		}
		var status struct {
			State Status `json:"state"`
		}
		decodeErr := json.NewDecoder(response.Body).Decode(&status)
		response.Body.Close()
		if decodeErr != nil {
			t.Fatal(decodeErr)
		}
		if status.State.Draining {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("drain never closed admission")
		}
		time.Sleep(time.Millisecond)
	}
	response, err = client.Do(request("POST", "/create?one=1"))
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(response.Body)
	response.Body.Close()
	if response.StatusCode != 503 || !strings.Contains(string(body), "workload_unavailable") || deliveries.Load() != 1 {
		t.Fatal("drained host listener forwarded/replayed application work")
	}
	select {
	case <-drained:
		t.Fatal("drain returned before stream completed")
	default:
	}
	close(finishStream)
	rest, err := io.ReadAll(stream.Body)
	stream.Body.Close()
	if err != nil || string(rest) != "end\n" {
		t.Fatal("deployment drain interrupted active response")
	}
	if <-drained != 204 {
		t.Fatal("completed drain refused")
	}
	if child.Process.Signal(syscall.SIGTERM) != nil || child.Wait() != nil {
		t.Fatal("old generation shutdown")
	}
	cancelBroker()
	if <-brokerResult != nil {
		t.Fatal("broker shutdown")
	}
	// New root-reviewed CID and image; the previous broker would reject this target.
	cid.Store(strings.Repeat("c", 64))
	image.Store("ghcr.io/xynes-studio/auth@sha256:" + strings.Repeat("d", 64))
	running.Store(true)
	endpointIP.Store(alternate)
	oldEndpointForeign.Store(true)
	newListener, err := net.Listen("tcp", alternate+":3100")
	if err != nil {
		t.Fatal(err)
	}
	newUpstream := &http.Server{Handler: commonHandler}
	go newUpstream.Serve(newListener)
	defer newUpstream.Close()
	cancelBroker, brokerResult = brokerStart()
	writeProxy()
	leaseClient.CloseIdleConnections()
	child = fixtureChild(t, "proxy", "^TestUnixChildProxy$")
	if child.Start() != nil {
		t.Fatal("new generation startup")
	}
	waitProxy()
	response, err = client.Do(request("POST", "/create?one=1"))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 503 {
		t.Fatal("rebound generation resumed itself")
	}
	if lease("/resume") != 204 {
		t.Fatal("new exact generation resume")
	}
	p.StartDrained = false
	writeProxy()
	// Abrupt restart preserves the original hold and observes the running CID.
	if child.Process.Kill() != nil || child.Wait() == nil {
		t.Fatal("owned proxy SIGKILL")
	}
	leaseClient.CloseIdleConnections()
	child = fixtureChild(t, "proxy", "^TestUnixChildProxy$")
	if child.Start() != nil {
		t.Fatal("new generation recovery")
	}
	waitProxy()
	persisted, err := os.ReadFile(p.StateFile)
	if err != nil {
		t.Fatal(err)
	}
	var state warmDocument
	if json.Unmarshal(persisted, &state) != nil || len(state.Leases) != 1 || state.Leases[0].Until != originalExpiry {
		t.Fatal("restart changed original durable expiry")
	}
	response, err = client.Do(request("POST", "/create?one=1"))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 201 || starts.Load() != 1 || stops.Load() != 0 || oldEndpointDeliveries.Load() != 0 {
		t.Fatal("generation restart woke/stopped/replayed unexpectedly")
	}
	if lease("/resume") != 204 || lease("/release") != 204 {
		t.Fatal("final validated release")
	}
	if child.Process.Signal(syscall.SIGTERM) != nil || child.Wait() != nil {
		t.Fatal("fixture cleanup drain")
	}
}
