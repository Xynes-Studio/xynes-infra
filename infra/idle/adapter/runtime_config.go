package idle

import (
	"bytes"
	"encoding/json"
	"io"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type BrokerConfig struct {
	Environment   string            `json:"environment"`
	ControlSocket string            `json:"control_socket"`
	DockerSocket  string            `json:"docker_socket"`
	TokenFile     string            `json:"token_file"`
	ProxyUID      int               `json:"proxy_uid"`
	SocketGID     int               `json:"socket_gid"`
	Targets       map[string]Target `json:"targets"`
}
type ProxyConfig struct {
	Environment   string      `json:"environment"`
	ControlSocket string      `json:"control_socket"`
	TokenFile     string      `json:"token_file"`
	Listen        string      `json:"listen"`
	Policy        ProxyPolicy `json:"policy"`
	IdleSeconds   int         `json:"idle_seconds"`
	WakeSeconds   int         `json:"wake_seconds"`
	MaxInFlight   int         `json:"max_in_flight"`
	StateFile     string      `json:"state_file"`
	HostNetwork   bool        `json:"host_network,omitempty"`
	StartDrained  bool        `json:"start_drained,omitempty"`
}

func stageUID(stage string) int {
	switch stage {
	case "qa":
		return 12002
	case "staging":
		return 12003
	case "prod":
		return 12004
	default:
		return -1
	}
}

func validControlPaths(stage, socket, token string) bool {
	return stageUID(stage) > 0 && socket == "/run/xynes/idle/"+stage+"/control.sock" && token == "/etc/xynes/idle/"+stage+"/control.token"
}

func (c BrokerConfig) Validate(uid int) error {
	if uid != 0 || c.ProxyUID != stageUID(c.Environment) || c.SocketGID != c.ProxyUID || c.DockerSocket != "/var/run/docker.sock" || !validControlPaths(c.Environment, c.ControlSocket, c.TokenFile) {
		return ErrDenied
	}
	client, err := NewUnixDockerClient(c.DockerSocket)
	if err != nil {
		return ErrDenied
	}
	_, err = NewBroker(client, c.Environment, c.Targets)
	return err
}

func (c ProxyConfig) managerPolicy() Policy {
	return Policy{IdleFor: time.Duration(c.IdleSeconds) * time.Second, WakeTimeout: time.Duration(c.WakeSeconds) * time.Second, MaxInFlight: c.MaxInFlight}
}

func (c ProxyConfig) groupSuffix() string {
	switch c.Policy.Group {
	case c.Environment + "-auth":
		return "auth"
	case c.Environment + "-cms":
		return "cms"
	default:
		return ""
	}
}

func (c ProxyConfig) leaseSocket() string {
	return "/run/xynes/idle/" + c.Environment + "/" + c.groupSuffix() + "/leases.sock"
}

func (c ProxyConfig) Validate(uid int) error {
	if uid != stageUID(c.Environment) || !validControlPaths(c.Environment, c.ControlSocket, c.TokenFile) || len(c.Policy.TrustedIngress) == 0 || c.IdleSeconds < 60 || c.IdleSeconds > 86400 || c.WakeSeconds < 1 || c.WakeSeconds > 120 {
		return ErrDenied
	}
	if c.groupSuffix() == "" || c.StateFile != "/var/lib/xynes/idle/"+c.Environment+"/"+c.groupSuffix()+"/warm.json" {
		return ErrDenied
	}
	expected := ""
	switch c.Policy.Group {
	case c.Environment + "-auth":
		expected = "http://auth_app_" + c.Environment + ":3100"
	case c.Environment + "-cms":
		expected = "http://cms_console_web_" + c.Environment + ":3000"
	default:
		return ErrDenied
	}
	if c.HostNetwork {
		if !c.Policy.DynamicTarget || !c.validHostNetwork() {
			return ErrDenied
		}
	} else if c.Listen != "0.0.0.0:8080" || c.Policy.Target != expected {
		return ErrDenied
	}
	provider, err := NewUnixControlClient(c.ControlSocket, strings.Repeat("0", 64))
	if err != nil {
		return ErrDenied
	}
	manager, err := NewManager(provider, map[string]Policy{c.Policy.Group: c.managerPolicy()}, time.Now)
	if err != nil {
		return ErrDenied
	}
	defer manager.Close()
	_, err = NewProxy(manager, c.Policy)
	return err
}

func decodeRuntimeConfig(data []byte, target any) error {
	if len(data) > 65536 {
		return ErrDenied
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(target) != nil || decoder.Decode(&struct{}{}) != io.EOF {
		return ErrDenied
	}
	return nil
}

func protectedMetadata(info os.FileInfo, owner uint32) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == owner && info.Mode().IsRegular() && info.Mode().Perm()&0037 == 0 && stat.Nlink == 1
}

// The open descriptor must match the protected, non-symlink inode both before
// and after the read. Production additionally verifies every parent is root
// owned and unwritable by other users; errors deliberately omit file content.
func readOwnedFile(path string, owner uint32, limit int64) ([]byte, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || limit < 1 {
		return nil, ErrDenied
	}
	before, err := os.Lstat(path)
	if err != nil || !protectedMetadata(before, owner) {
		return nil, ErrDenied
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, ErrDenied
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !protectedMetadata(opened, owner) || !os.SameFile(before, opened) {
		return nil, ErrDenied
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	after, statErr := os.Lstat(path)
	if err != nil || statErr != nil || len(data) > int(limit) || !protectedMetadata(after, owner) || !os.SameFile(opened, after) {
		return nil, ErrDenied
	}
	return data, nil
}

func rootParents(path string) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return ErrDenied
	}
	for dir := filepath.Dir(path); ; dir = filepath.Dir(dir) {
		info, err := os.Lstat(dir)
		if err != nil || !info.IsDir() || info.Mode().Perm()&0022 != 0 {
			return ErrDenied
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != 0 {
			return ErrDenied
		}
		if dir == "/" {
			return nil
		}
	}
}

// LoadRuntimeConfig accepts only protected root-authored configuration files.
func LoadRuntimeConfig(path string, target any) error {
	if err := rootParents(path); err != nil {
		return ErrDenied
	}
	data, err := readOwnedFile(path, 0, 65536)
	if err != nil {
		return ErrDenied
	}
	return decodeRuntimeConfig(data, target)
}

func loadStageToken(path string, gid int) (string, error) {
	if rootParents(path) != nil {
		return "", ErrDenied
	}
	data, err := readOwnedFile(path, 0, 65)
	if err != nil {
		return "", ErrDenied
	}
	info, err := os.Lstat(path)
	if err != nil {
		return "", ErrDenied
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Gid) != gid || info.Mode().Perm()&0200 != 0 {
		return "", ErrDenied
	}
	token := strings.TrimSuffix(string(data), "\n")
	if !tokenPattern.MatchString(token) {
		return "", ErrDenied
	}
	return token, nil
}

func (c ProxyConfig) validHostNetwork() bool {
	base := map[string]int{"qa": 18080, "staging": 18180, "prod": 18280}[c.Environment]
	suffix := c.groupSuffix()
	offset, upstream := 1, 3100
	if suffix == "cms" {
		offset, upstream = 2, 3000
	}
	if suffix == "" || base == 0 {
		return false
	}
	listen, err := netip.ParseAddrPort(c.Listen)
	if err != nil || !listen.Addr().Is4() || !listen.Addr().IsPrivate() || int(listen.Port()) != base+offset {
		return false
	}
	target, err := url.Parse(c.Policy.Target)
	if err != nil || target.Scheme != "http" || target.User != nil || target.Path != "" || target.RawQuery != "" || target.Fragment != "" || target.Port() != strconv.Itoa(upstream) {
		return false
	}
	address, err := netip.ParseAddr(target.Hostname())
	return err == nil && address.Is4() && address.IsPrivate()
}
