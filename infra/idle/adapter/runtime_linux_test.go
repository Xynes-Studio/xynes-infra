//go:build linux

package idle

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func TestUnixChildControl(t *testing.T) {
	if os.Getenv("XYNES_IDLE_CHILD") != "control" {
		t.Skip("fixture child only")
	}
	client, err := NewUnixControlClient("/run/xynes/idle/qa/control.sock", strings.Repeat("f", 64))
	if err != nil {
		t.Fatal(err)
	}
	if err := client.EnsureReady(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
	if err := client.Stop(context.Background(), "prod-auth"); err != ErrDenied {
		t.Fatal("foreign group accepted")
	}
	if err := client.Stop(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
}

func TestUnixChildProxy(t *testing.T) {
	if os.Getenv("XYNES_IDLE_CHILD") != "proxy" {
		t.Skip("fixture child only")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM)
	defer cancel()
	var config ProxyConfig
	if LoadRuntimeConfig("/etc/xynes/idle/qa/proxy.json", &config) != nil {
		t.Fatal("protected proxy config unreadable")
	}
	if err := RunProxy(ctx, config); err != nil {
		t.Fatal(err)
	}
}

func TestUnixChildWarmDenied(t *testing.T) {
	if os.Getenv("XYNES_IDLE_CHILD") != "warm-denied" {
		t.Skip("fixture child only")
	}
	client, err := unixHTTPClient("/run/xynes/idle/qa/auth/leases.sock")
	if err != nil {
		t.Fatal(err)
	}
	r, _ := http.NewRequest("POST", "http://lease/warm", strings.NewReader(`{"group":"qa-auth","owner":"foreign","ttl_seconds":3600}`))
	r.Header.Set("Authorization", "Bearer "+strings.Repeat("f", 64))
	response, err := client.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 403 {
		t.Fatal("stage proxy peer gained root lease authority")
	}
}

func fixtureChild(t *testing.T, role, pattern string) *exec.Cmd {
	t.Helper()
	command := exec.Command(os.Args[0], "-test.run", pattern, "-test.v")
	if dir := os.Getenv("XYNES_IDLE_COVER_DIR"); dir != "" {
		command.Args = append(command.Args, "-test.gocoverdir", dir, "-test.coverprofile", dir+"/child-"+role+".out")
	}
	command.Env = append(os.Environ(), "XYNES_IDLE_CHILD="+role)
	command.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 12002, Gid: 12002}}
	return command
}

func TestLinuxBrokerProxyRolesThroughPrivateUnixBoundary(t *testing.T) {
	if os.Geteuid() != 0 || os.Getenv("XYNES_IDLE_UNIX_FIXTURE") != "1" {
		t.Skip("requires disposable Linux root fixture")
	}
	for _, dir := range []string{"/run/xynes/idle/qa", "/etc/xynes/idle/qa", "/var/lib/xynes/idle/qa"} {
		if os.MkdirAll(dir, 0750) != nil || os.Chown(dir, 0, 12002) != nil {
			t.Fatal("fixture metadata setup failed")
		}
	}
	for _, dir := range []string{"/run/xynes", "/run/xynes/idle", "/etc/xynes/idle", "/var/lib/xynes/idle"} {
		if os.Chmod(dir, 0755) != nil {
			t.Fatal("fixture parent traversal setup failed")
		}
	}
	for _, dir := range []string{"/run/xynes/idle/qa/auth", "/var/lib/xynes/idle/qa/auth"} {
		if os.Mkdir(dir, 0700) != nil || os.Chown(dir, 12002, 12002) != nil {
			t.Fatal("fixture private lease/state metadata failed")
		}
	}
	tokenFile := "/etc/xynes/idle/qa/control.token"
	if os.WriteFile(tokenFile, []byte(strings.Repeat("f", 64)), 0440) != nil || os.Chown(tokenFile, 0, 12002) != nil {
		t.Fatal("fixture token setup failed")
	}
	b := validBrokerConfig()
	p := validProxyConfig()
	p.Policy.TrustedIngress = []string{"127.0.0.1/32"}
	configData, _ := json.Marshal(p)
	if os.WriteFile("/etc/xynes/idle/qa/proxy.json", configData, 0640) != nil || os.Chown("/etc/xynes/idle/qa/proxy.json", 0, 12002) != nil {
		t.Fatal("fixture config setup failed")
	}
	var running atomic.Bool
	var starts, stops, deliveries atomic.Int32
	daemon, err := net.Listen("unix", "/var/run/docker.sock")
	if err != nil {
		t.Fatal(err)
	}
	if os.Chmod("/var/run/docker.sock", 0600) != nil {
		t.Fatal("mock daemon socket permissions failed")
	}
	defer daemon.Close()
	target := b.Targets["qa-auth"]
	docker := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		prefix := "/v1.49/containers/" + target.ContainerID
		switch {
		case r.Method == "GET" && r.URL.Path == prefix+"/json":
			fmt.Fprintf(w, `{"Id":%q,"Config":{"Image":%q,"Labels":{"com.xynes.managed":"true","com.xynes.environment":"qa","com.xynes.role":"application","com.docker.compose.service":"auth-app"}},"State":{"Running":%t,"Health":{"Status":"healthy"}}}`, target.ContainerID, target.Image, running.Load())
		case r.Method == "POST" && r.URL.Path == prefix+"/start":
			running.Store(true)
			starts.Add(1)
			w.WriteHeader(204)
		case r.Method == "POST" && r.URL.Path == prefix+"/stop" && r.URL.RawQuery == "t=60":
			running.Store(false)
			stops.Add(1)
			w.WriteHeader(204)
		default:
			t.Error("unexpected daemon operation")
			w.WriteHeader(403)
		}
	})}
	go docker.Serve(daemon)
	defer docker.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() { result <- RunBroker(ctx, b) }()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Lstat(b.ControlSocket); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("broker socket not ready")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if output, err := fixtureChild(t, "control", "^TestUnixChildControl$").CombinedOutput(); err != nil {
		t.Fatalf("authorized peer control failed: %s", output)
	}
	if starts.Load() != 1 || stops.Load() != 1 {
		t.Fatal("bounded lifecycle roundtrip changed")
	}
	upstreamListener, err := net.Listen("tcp", "127.0.0.1:3100")
	if err != nil {
		t.Fatal(err)
	}
	streamFinish := make(chan struct{})
	upstream := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" && r.URL.Path == "/stream" {
			w.WriteHeader(200)
			w.Write([]byte("started\n"))
			w.(http.Flusher).Flush()
			select {
			case <-streamFinish:
				w.Write([]byte("finished\n"))
			case <-r.Context().Done():
			}
			return
		}
		if r.Method != "POST" || r.URL.RawQuery != "fixture=1" || r.Header.Get("Authorization") != "Bearer fixture-user" {
			t.Error("original request changed")
		}
		deliveries.Add(1)
		w.WriteHeader(201)
		w.Write([]byte("created"))
	})}
	go upstream.Serve(upstreamListener)
	defer upstream.Close()
	child := fixtureChild(t, "proxy", "^TestUnixChildProxy$")
	if child.Start() != nil {
		t.Fatal("unprivileged proxy start failed")
	}
	defer func() {
		if child.ProcessState == nil {
			child.Process.Kill()
			child.Wait()
		}
	}()
	client := &http.Client{Timeout: 3 * time.Second}
	waitProxy := func() {
		deadline = time.Now().Add(5 * time.Second)
		for {
			r, _ := http.NewRequest("GET", "http://127.0.0.1:8080/health", nil)
			r.Host = "auth.qa.xynes.com"
			response, e := client.Do(r)
			if e == nil {
				response.Body.Close()
				if response.StatusCode == 200 {
					break
				}
			}
			if time.Now().After(deadline) {
				t.Fatal("proxy did not start")
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
	waitProxy()
	upgrade, _ := http.NewRequest("GET", "http://127.0.0.1:8080/socket", nil)
	upgrade.Host = "auth.qa.xynes.com"
	upgrade.Header.Set("Connection", "Upgrade")
	upgrade.Header.Set("Upgrade", "websocket")
	upgradeResponse, err := client.Do(upgrade)
	if err != nil {
		t.Fatal(err)
	}
	upgradeResponse.Body.Close()
	if upgradeResponse.StatusCode != 501 || starts.Load() != 1 || deliveries.Load() != 0 {
		t.Fatal("runtime upgrade bypassed work boundary")
	}
	request, _ := http.NewRequest("POST", "http://127.0.0.1:8080/create?fixture=1", strings.NewReader("fixture-body"))
	request.Host = "auth.qa.xynes.com"
	request.Header.Set("Authorization", "Bearer fixture-user")
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 201 || deliveries.Load() != 1 || starts.Load() != 2 {
		t.Fatal("first-use dispatch failed")
	}
	leaseClient, err := unixHTTPClient(p.leaseSocket())
	if err != nil {
		t.Fatal(err)
	}
	callLease := func(path, body string) int {
		r, _ := http.NewRequest("POST", "http://lease"+path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+strings.Repeat("f", 64))
		response, err := leaseClient.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		return response.StatusCode
	}
	if output, err := fixtureChild(t, "warm-denied", "^TestUnixChildWarmDenied$").CombinedOutput(); err != nil {
		t.Fatalf("private lease peer denial failed: %s", output)
	}
	if callLease("/warm", `{"group":"qa-auth","owner":"deploy-runtime","ttl_seconds":3600}`) != 204 {
		t.Fatal("root deployment lease failed")
	}
	if info, err := os.Stat(p.StateFile); err != nil || info.Mode().Perm() != 0600 || info.Sys().(*syscall.Stat_t).Uid != 12002 {
		t.Fatal("runtime warm state metadata unsafe")
	}
	streamRequest, _ := http.NewRequest("GET", "http://127.0.0.1:8080/stream", nil)
	streamRequest.Host = "auth.qa.xynes.com"
	streamClient := &http.Client{Timeout: 15 * time.Second}
	streamResponse, err := streamClient.Do(streamRequest)
	if err != nil {
		t.Fatal(err)
	}
	defer streamResponse.Body.Close()
	started := make([]byte, len("started\n"))
	if _, err := io.ReadFull(streamResponse.Body, started); err != nil || string(started) != "started\n" {
		t.Fatal("actual stream did not start")
	}
	if child.Process.Signal(syscall.SIGTERM) != nil {
		t.Fatal("fixture cannot signal its owned child")
	}
	deadline = time.Now().Add(5 * time.Second)
	for {
		check, _ := http.NewRequest("GET", "http://127.0.0.1:8080/health", nil)
		check.Host = "auth.qa.xynes.com"
		response, err := client.Do(check)
		if err != nil {
			break
		}
		response.Body.Close()
		if time.Now().After(deadline) {
			t.Fatal("old proxy did not quiesce")
		}
		time.Sleep(10 * time.Millisecond)
	}
	output, restartErr := fixtureChild(t, "proxy", "^TestUnixChildProxy$").CombinedOutput()
	if restartErr == nil || !strings.Contains(string(output), ErrBusy.Error()) {
		t.Fatalf("replacement not denied during active stream: %s", output)
	}
	close(streamFinish)
	finished, err := io.ReadAll(streamResponse.Body)
	if err != nil || string(finished) != "finished\n" {
		t.Fatal("shutdown interrupted actual stream")
	}
	if child.Wait() != nil {
		t.Fatal("proxy did not drain cleanly")
	}
	if stops.Load() != 1 {
		t.Fatal("proxy shutdown stopped workload")
	}
	child = fixtureChild(t, "proxy", "^TestUnixChildProxy$")
	if child.Start() != nil {
		t.Fatal("proxy restart failed")
	}
	waitProxy()
	// Abrupt process death leaves a recorded socket inode and durable hold.
	if child.Process.Kill() != nil || child.Wait() == nil {
		t.Fatal("owned proxy SIGKILL was not observed")
	}
	if info, err := os.Lstat(p.leaseSocket()); err != nil || info.Mode()&os.ModeSocket == 0 {
		t.Fatal("SIGKILL fixture did not leave a stale socket")
	}
	leaseClient.CloseIdleConnections()
	child = fixtureChild(t, "proxy", "^TestUnixChildProxy$")
	if child.Start() != nil {
		t.Fatal("proxy crash recovery start failed")
	}
	waitProxy()
	// Success proves the exact owned hold was restored into the new process.
	if callLease("/release", `{"group":"qa-auth","owner":"deploy-runtime"}`) != 204 {
		t.Fatal("proxy restart lost owned deployment hold")
	}
	if child.Process.Signal(syscall.SIGTERM) != nil || child.Wait() != nil {
		t.Fatal("restarted proxy shutdown failed")
	}
	if stops.Load() != 1 {
		t.Fatal("restart or shutdown stopped held workload")
	}
	cancel()
	if err := <-result; err != nil {
		t.Fatal("broker did not drain cleanly")
	}
}
