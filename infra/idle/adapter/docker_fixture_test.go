package idle

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// Opt-in only: one freshly named, labelled synthetic container, no host data or
// socket mounts, no real credentials, and no mutations to existing workloads.
func TestOwnedDockerFixtureColdWritesAndLongWork(t *testing.T) {
	if os.Getenv("XYNES_IDLE_DOCKER_FIXTURE") != "1" {
		t.Skip("explicit disposable Docker fixture opt-in required")
	}
	socket := os.Getenv("XYNES_IDLE_DOCKER_SOCKET")
	if socket == "" {
		t.Fatal("explicit Docker socket required")
	}
	image := "node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e"
	name := fmt.Sprintf("apex-idle-adapter-fixture-%x", time.Now().UnixNano())
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	_ = listener.Close()
	script := `const http=require('http');const started=Date.now();const counts=new Map();const memory=Buffer.alloc(48*1024*1024,1);const server=http.createServer((req,res)=>{if(req.url==='/ready'){res.writeHead(Date.now()-started>250?200:503);res.end('ready');return;}let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{const id=req.headers['x-test-id']||'none';counts.set(id,(counts.get(id)||0)+1);const reply=()=>{res.writeHead(201,{'content-type':'application/json'});res.end(JSON.stringify({method:req.method,uri:req.url,body,auth:req.headers.authorization,count:counts.get(id)}));};if(req.url==='/long'){setTimeout(reply,2500);}else{reply();}});});server.listen(8080,'0.0.0.0');process.on('SIGTERM',()=>server.close(()=>process.exit(0)));`
	command := func(args ...string) string {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()
		out, err := exec.CommandContext(ctx, "docker", args...).CombinedOutput()
		if err != nil {
			t.Fatalf("disposable Docker operation failed (%s)", args[0])
		}
		return strings.TrimSpace(string(out))
	}
	command("create", "--platform", "linux/amd64", "--name", name, "--label", "com.xynes.apex.adapter=disposable", "--label", "com.xynes.managed=true", "--label", "com.xynes.environment=qa", "--label", "com.xynes.role=application", "--label", "com.docker.compose.service=auth-app", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=64m,mode=1777", "--user", "1000:1000", "--memory", "256m", "--cpus", "0.75", "--pids-limit", "128", "--init", "-p", address+":8080", "--health-cmd", `node -e "fetch('http://127.0.0.1:8080/ready').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"`, "--health-interval", "1s", "--health-start-period", "2s", "--health-start-interval", "100ms", "--health-timeout", "3s", "--health-retries", "1", image, "node", "-e", script)
	t.Cleanup(func() { _ = exec.Command("docker", "rm", "-f", name).Run() })
	id := command("inspect", "--format", "{{.Id}}", name)
	containerClient, err := NewUnixDockerClient(socket)
	if err != nil {
		t.Fatal(err)
	}
	control, err := NewBroker(containerClient, "qa", map[string]Target{"qa-auth": {ContainerID: id, Image: image, Service: "auth-app"}})
	if err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("f", 64)
	handler, err := NewControlHandler(control, token)
	if err != nil {
		t.Fatal(err)
	}
	private := httptest.NewServer(handler)
	defer private.Close()
	provider := newControlHTTPClient(private.URL, token, private.Client())
	manager, err := NewManager(provider, map[string]Policy{"qa-auth": {IdleFor: 200 * time.Millisecond, WakeTimeout: 30 * time.Second, MaxInFlight: 8}}, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	defer manager.Close()
	// Reserve one unused loopback port for this fixture so the FIRST request can
	// wake it. No external warm command is allowed before a cold-write cycle.
	for i := 0; i < 20; i++ {
		proxy, err := NewProxy(manager, ProxyPolicy{Group: "qa-auth", Target: "http://" + address, Hosts: []string{"qa-auth.example.invalid"}, MaxBodyBytes: 1024, MaxQueuedRequests: 8})
		if err != nil {
			t.Fatal(err)
		}
		response := httptest.NewRecorder()
		request := appRequest("POST", "/echo?state=synthetic", "unchanged")
		request.Header.Set("X-Test-ID", fmt.Sprint(i))
		proxy.ServeHTTP(response, request)
		var body struct {
			Method, URI, Body, Auth string
			Count                   int
		}
		if json.Unmarshal(response.Body.Bytes(), &body) != nil {
			t.Fatal("fixture response invalid")
		}
		if response.Code != 201 || body.Method != "POST" || body.URI != "/echo?state=synthetic" || body.Body != "unchanged" || body.Auth != "Bearer synthetic-fixture" || body.Count != 1 {
			t.Fatal("cold write semantics changed")
		}
		time.Sleep(250 * time.Millisecond)
		if err := manager.Tick(context.Background()); err != nil {
			t.Fatal(err)
		}
		info, err := containerClient.Inspect(context.Background(), id)
		if err != nil || info.Running {
			t.Fatal("fixture did not stop after completed idle")
		}
	}
	proxy, err := NewProxy(manager, ProxyPolicy{Group: "qa-auth", Target: "http://" + address, Hosts: []string{"qa-auth.example.invalid"}, MaxBodyBytes: 1024, MaxQueuedRequests: 8})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(proxy)
	defer server.Close()
	request, err := http.NewRequest("POST", server.URL+"/long", strings.NewReader("long"))
	if err != nil {
		t.Fatal(err)
	}
	request.Host = "qa-auth.example.invalid"
	finished := make(chan error, 1)
	go func() {
		response, err := server.Client().Do(request)
		if err == nil {
			_, err = io.ReadAll(response.Body)
			_ = response.Body.Close()
			if response.StatusCode != 201 {
				err = ErrUnavailable
			}
		}
		finished <- err
	}()
	deadline := time.Now().Add(30 * time.Second)
	for {
		status, _ := manager.Snapshot("qa-auth")
		if status.Phase == "awake_last_seen" && status.Active > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("long fixture did not become active")
		}
		time.Sleep(20 * time.Millisecond)
	}
	time.Sleep(700 * time.Millisecond)
	if err := manager.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	info, err := containerClient.Inspect(context.Background(), id)
	if err != nil || !info.Running {
		t.Fatal("long real request stopped")
	}
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
	t.Log("20 real Docker completion/idle cycles and long work passed; synthetic fixture only")
}
