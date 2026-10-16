package idle

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"time"
)

type DockerClient struct {
	endpoint string
	http     *http.Client
}

// NewUnixDockerClient belongs only in the root-owned private broker process.
// The public request proxy gets a narrow lease client, never this transport.
func NewUnixDockerClient(socket string) (*DockerClient, error) {
	client, err := unixHTTPClient(socket)
	if err != nil {
		return nil, err
	}
	return newContainerHTTPClient("http://docker", client), nil
}

func unixHTTPClient(socket string) (*http.Client, error) {
	if !filepath.IsAbs(socket) || filepath.Clean(socket) != socket {
		return nil, ErrDenied
	}
	dialer := net.Dialer{Timeout: 3 * time.Second}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return dialer.DialContext(ctx, "unix", socket)
	}, MaxConnsPerHost: 4, IdleConnTimeout: 30 * time.Second}
	client := &http.Client{Transport: transport, Timeout: 90 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	return client, nil
}

func newContainerHTTPClient(endpoint string, client *http.Client) *DockerClient {
	return &DockerClient{endpoint: endpoint, http: client}
}

func (d *DockerClient) request(ctx context.Context, method, id, suffix string) (*http.Response, error) {
	if !containerPattern.MatchString(id) {
		return nil, ErrDenied
	}
	request, err := http.NewRequestWithContext(ctx, method, d.endpoint+"/v1.49/containers/"+id+suffix, nil)
	if err != nil {
		return nil, ErrUnavailable
	}
	response, err := d.http.Do(request)
	if err != nil {
		return nil, ErrUnavailable
	}
	return response, nil
}

func (d *DockerClient) Inspect(ctx context.Context, id string) (ContainerInfo, error) {
	response, err := d.request(ctx, "GET", id, "/json")
	if err != nil {
		return ContainerInfo{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return ContainerInfo{}, ErrUnavailable
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+1))
	if err != nil || len(body) > 1024*1024 {
		return ContainerInfo{}, ErrUnavailable
	}
	var raw struct {
		ID     string `json:"Id"`
		Config struct {
			Image  string
			Labels map[string]string
		}
		NetworkSettings struct{ Networks map[string]NetworkEndpoint }
		State           struct {
			Running bool
			Health  struct{ Status string }
		}
	}
	if json.Unmarshal(body, &raw) != nil {
		return ContainerInfo{}, ErrUnavailable
	}
	if raw.ID != id {
		return ContainerInfo{}, ErrDenied
	}
	labels := make(map[string]string)
	for _, key := range []string{"com.xynes.managed", "com.xynes.environment", "com.xynes.role", "com.docker.compose.service"} {
		if value, exists := raw.Config.Labels[key]; exists && len(value) <= 128 {
			labels[key] = value
		}
	}
	if len(raw.NetworkSettings.Networks) > 16 {
		return ContainerInfo{}, ErrUnavailable
	}
	networks := make(map[string]NetworkEndpoint)
	for name, network := range raw.NetworkSettings.Networks {
		if len(name) > 128 || len(network.NetworkID) > 64 || len(network.IPAddress) > 45 {
			return ContainerInfo{}, ErrUnavailable
		}
		networks[name] = network
	}
	return ContainerInfo{ID: raw.ID, Image: raw.Config.Image, Running: raw.State.Running, Healthy: raw.State.Health.Status == "healthy", Labels: labels, Networks: networks}, nil
}

func (d *DockerClient) action(ctx context.Context, id, suffix string) error {
	response, err := d.request(ctx, "POST", id, suffix)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusNoContent && response.StatusCode != http.StatusNotModified {
		return ErrUnavailable
	}
	return nil
}

func (d *DockerClient) Start(ctx context.Context, id string) error {
	return d.action(ctx, id, "/start")
}
func (d *DockerClient) Stop(ctx context.Context, id string) error {
	return d.action(ctx, id, "/stop?t=60")
}
