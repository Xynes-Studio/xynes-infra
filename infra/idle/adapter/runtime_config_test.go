package idle

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func validBrokerConfig() BrokerConfig {
	return BrokerConfig{Environment: "qa", ControlSocket: "/run/xynes/idle/qa/control.sock", DockerSocket: "/var/run/docker.sock", TokenFile: "/etc/xynes/idle/qa/control.token", ProxyUID: 12002, SocketGID: 12002, Targets: map[string]Target{"qa-auth": {ContainerID: strings.Repeat("a", 64), Image: "ghcr.io/xynes-studio/auth@sha256:" + strings.Repeat("b", 64), Service: "auth-app"}}}
}

func validProxyConfig() ProxyConfig {
	return ProxyConfig{Environment: "qa", ControlSocket: "/run/xynes/idle/qa/control.sock", TokenFile: "/etc/xynes/idle/qa/control.token", Listen: "0.0.0.0:8080", Policy: ProxyPolicy{Group: "qa-auth", Target: "http://auth_app_qa:3100", Hosts: []string{"auth.qa.xynes.com"}, MaxBodyBytes: 1048576, MaxQueuedRequests: 32, TrustedIngress: []string{"172.20.0.2/32"}}, IdleSeconds: 900, WakeSeconds: 30, MaxInFlight: 32, StateFile: "/var/lib/xynes/idle/qa/auth/warm.json"}
}

func TestProxyConfigRequiresEnvironmentAndGroupOwnedDurableState(t *testing.T) {
	for _, path := range []string{"", "/tmp/warm.json", "/var/lib/xynes/idle/prod/auth/warm.json", "/var/lib/xynes/idle/qa/cms/warm.json"} {
		config := validProxyConfig()
		config.StateFile = path
		if config.Validate(12002) == nil {
			t.Fatal("unsafe warm state path accepted")
		}
	}
}

func TestRuntimeConfigSeparatesPrivilegesAndEnvironment(t *testing.T) {
	b := validBrokerConfig()
	p := validProxyConfig()
	if b.Validate(0) != nil || p.Validate(12002) != nil {
		t.Fatal("approved runtime roles rejected")
	}
	if b.Validate(12002) == nil || p.Validate(0) == nil {
		t.Fatal("incorrect process privilege accepted")
	}
	badB := []BrokerConfig{b, b, b, b, b}
	badB[0].ProxyUID = 12004
	badB[1].ControlSocket = "/run/xynes/idle/prod/control.sock"
	badB[2].DockerSocket = "/tmp/arbitrary.sock"
	badB[3].TokenFile = "/etc/xynes/idle/prod/control.token"
	badB[4].SocketGID = 0
	for _, config := range badB {
		if config.Validate(0) == nil {
			t.Fatal("foreign privileged boundary accepted")
		}
	}
	badP := []ProxyConfig{p, p, p, p, p, p}
	badP[0].Policy.Target = "http://auth_app_prod:3100"
	badP[1].Policy.Group = "qa-gateway"
	badP[2].Policy.TrustedIngress = nil
	badP[3].Listen = "0.0.0.0:443"
	badP[4].IdleSeconds = 0
	badP[5].Policy.Target = "http://auth_app_qa:3100/private"
	for _, config := range badP {
		if config.Validate(12002) == nil {
			t.Fatal("unsafe ingress configuration accepted")
		}
	}
}

func TestRuntimeJSONRejectsPrivilegedFieldsAndTrailingData(t *testing.T) {
	b, _ := json.Marshal(validProxyConfig())
	var p ProxyConfig
	if decodeRuntimeConfig(b, &p) != nil {
		t.Fatal("approved strict configuration rejected")
	}
	for _, input := range [][]byte{append(b, []byte(` {}`)...), []byte(`{"docker_socket":"/var/run/docker.sock"}`), []byte(strings.Repeat("x", 65537))} {
		if decodeRuntimeConfig(input, &p) == nil {
			t.Fatal("unsafe or oversized config accepted")
		}
	}
}

func TestProtectedRuntimeFileRejectsSymlinksAndWritableSecrets(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "token")
	if err := os.WriteFile(file, []byte("fixture-secret"), 0600); err != nil {
		t.Fatal(err)
	}
	if b, err := readOwnedFile(file, uint32(os.Geteuid()), 64); err != nil || string(b) != "fixture-secret" {
		t.Fatal("protected file rejected")
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(file, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readOwnedFile(link, uint32(os.Geteuid()), 64); err == nil {
		t.Fatal("symlink secret accepted")
	}
	if err := os.Chmod(file, 0620); err != nil {
		t.Fatal(err)
	}
	if _, err := readOwnedFile(file, uint32(os.Geteuid()), 64); err == nil {
		t.Fatal("group writable secret accepted")
	}
	if err := os.Chmod(file, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readOwnedFile(file, uint32(os.Geteuid()), 4); err == nil {
		t.Fatal("oversized file accepted")
	}
	if _, err := readOwnedFile(file, uint32(os.Geteuid()+1), 64); err == nil {
		t.Fatal("foreign owner accepted")
	}
}
