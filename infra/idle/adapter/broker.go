package idle

import (
	"context"
	"net/netip"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// ContainerInfo deliberately excludes raw Docker environment, mount paths and
// credentials. Concrete clients must never forward arbitrary inspect JSON.
type NetworkEndpoint struct {
	NetworkID string
	IPAddress string
}

type ContainerInfo struct {
	ID       string
	Image    string
	Running  bool
	Healthy  bool
	Labels   map[string]string
	Networks map[string]NetworkEndpoint
}

type ContainerClient interface {
	Inspect(context.Context, string) (ContainerInfo, error)
	Start(context.Context, string) error
	Stop(context.Context, string) error
}

type Target struct {
	ContainerID string `json:"container_id"`
	Image       string `json:"image"`
	Service     string `json:"service"`
	NetworkID   string `json:"network_id,omitempty"`
}
type Broker struct {
	client      ContainerClient
	environment string
	targets     map[string]Target
}

var containerPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var imagePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$`)

// NewBroker enables only audited frontend processes for the MVP. Core/data/job
// groups cannot become sleepable merely by acquiring an opt-in Docker label.
func NewBroker(client ContainerClient, environment string, targets map[string]Target) (*Broker, error) {
	if client == nil || (environment != "qa" && environment != "staging" && environment != "prod") || len(targets) < 1 || len(targets) > 2 {
		return nil, ErrDenied
	}
	b := &Broker{client: client, environment: environment, targets: make(map[string]Target, len(targets))}
	ids := map[string]bool{}
	for name, target := range targets {
		suffix := ""
		switch target.Service {
		case "auth-app":
			suffix = "auth"
		case "cms-console-web":
			suffix = "cms"
		default:
			return nil, ErrDenied
		}
		if name != environment+"-"+suffix || !containerPattern.MatchString(target.ContainerID) || !imagePattern.MatchString(target.Image) || ids[target.ContainerID] || (target.NetworkID != "" && !containerPattern.MatchString(target.NetworkID)) {
			return nil, ErrDenied
		}
		ids[target.ContainerID] = true
		b.targets[name] = target
	}
	return b, nil
}

func (b *Broker) inspect(ctx context.Context, target Target) (ContainerInfo, error) {
	if ctx.Err() != nil {
		return ContainerInfo{}, ErrUnavailable
	}
	info, err := b.client.Inspect(ctx, target.ContainerID)
	if err != nil {
		return ContainerInfo{}, ErrUnavailable
	}
	if info.ID != target.ContainerID || info.Image != target.Image || info.Labels["com.xynes.managed"] != "true" || info.Labels["com.xynes.environment"] != b.environment || info.Labels["com.xynes.role"] != "application" || info.Labels["com.docker.compose.service"] != target.Service {
		return ContainerInfo{}, ErrDenied
	}
	return info, nil
}

func (b *Broker) EnsureReady(ctx context.Context, name string) error {
	target, ok := b.targets[name]
	if !ok {
		return ErrDenied
	}
	bounded, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	info, err := b.inspect(bounded, target)
	if err != nil {
		return err
	}
	if !info.Running {
		if err := b.client.Start(bounded, target.ContainerID); err != nil {
			return ErrUnavailable
		}
	}
	for {
		info, err = b.inspect(bounded, target)
		if err != nil {
			return err
		}
		if info.Running && info.Healthy {
			return nil
		}
		timer := time.NewTimer(100 * time.Millisecond)
		select {
		case <-bounded.Done():
			timer.Stop()
			return ErrUnavailable
		case <-timer.C:
		}
	}
}

func (b *Broker) Stop(ctx context.Context, name string) error {
	target, ok := b.targets[name]
	if !ok {
		return ErrDenied
	}
	if !strings.HasPrefix(name, b.environment+"-") {
		return ErrDenied
	}
	info, err := b.inspect(ctx, target)
	if err != nil {
		return err
	}
	if !info.Running {
		return nil
	}
	if err := b.client.Stop(ctx, target.ContainerID); err != nil {
		return ErrUnavailable
	}
	return nil
}

// Observe checks the exact approved generation without starting or stopping it.
func (b *Broker) Observe(ctx context.Context, name string) (bool, error) {
	target, ok := b.targets[name]
	if !ok {
		return false, ErrDenied
	}
	info, err := b.inspect(ctx, target)
	if err != nil {
		return false, err
	}
	if info.Running && info.Healthy && target.NetworkID != "" {
		if _, err := b.Endpoint(ctx, name); err != nil {
			return false, err
		}
	}
	return info.Running && info.Healthy, nil
}

// Endpoint resolves only the approved running generation on its exact stage
// network. It never trusts a cached IP that Docker may reuse after a stop.
func (b *Broker) Endpoint(ctx context.Context, name string) (string, error) {
	target, ok := b.targets[name]
	if !ok || target.NetworkID == "" {
		return "", ErrDenied
	}
	info, err := b.inspect(ctx, target)
	if err != nil {
		return "", err
	}
	if !info.Running || !info.Healthy {
		return "", ErrUnavailable
	}
	network, ok := info.Networks["xynes_"+b.environment+"_net"]
	if !ok || network.NetworkID != target.NetworkID {
		return "", ErrDenied
	}
	address, err := netip.ParseAddr(network.IPAddress)
	if err != nil || !address.Is4() || !address.IsPrivate() {
		return "", ErrDenied
	}
	port := 3100
	if target.Service == "cms-console-web" {
		port = 3000
	}
	return "http://" + address.String() + ":" + strconv.Itoa(port), nil
}
