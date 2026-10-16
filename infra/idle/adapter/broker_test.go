package idle

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

type fixtureDocker struct {
	mu       sync.Mutex
	info     ContainerInfo
	inspects []string
	starts   []string
	stops    []string
	fail     bool
}

func (d *fixtureDocker) Inspect(_ context.Context, id string) (ContainerInfo, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.inspects = append(d.inspects, id)
	if d.fail {
		return ContainerInfo{}, errors.New("PRIVATE-DOCKER-MATERIAL")
	}
	return d.info, nil
}
func (d *fixtureDocker) Start(_ context.Context, id string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.starts = append(d.starts, id)
	d.info.Running = true
	return nil
}
func (d *fixtureDocker) Stop(_ context.Context, id string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.stops = append(d.stops, id)
	d.info.Running = false
	return nil
}

func approvedTarget() Target {
	return Target{ContainerID: strings.Repeat("a", 64), Image: "registry.example.invalid/xynes/auth-app@sha256:" + strings.Repeat("b", 64), Service: "auth-app"}
}

func approvedContainer() ContainerInfo {
	target := approvedTarget()
	return ContainerInfo{ID: target.ContainerID, Image: target.Image, Running: false, Healthy: true, Labels: map[string]string{"com.xynes.managed": "true", "com.xynes.environment": "qa", "com.xynes.role": "application", "com.docker.compose.service": "auth-app"}}
}

func broker(t *testing.T, d *fixtureDocker) *Broker {
	t.Helper()
	b, err := NewBroker(d, "qa", map[string]Target{"qa-auth": approvedTarget()})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestBrokerStartsOnlyExactApprovedContainerAndWaitsForHealth(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	b := broker(t, d)
	if err := b.EnsureReady(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
	if len(d.starts) != 1 || d.starts[0] != approvedTarget().ContainerID {
		t.Fatal("wrong container started")
	}
	if err := b.EnsureReady(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
	if len(d.starts) != 1 {
		t.Fatal("running service restarted")
	}
	if err := b.Stop(context.Background(), "qa-auth"); err != nil {
		t.Fatal(err)
	}
	if len(d.stops) != 1 || d.stops[0] != approvedTarget().ContainerID {
		t.Fatal("wrong container stopped")
	}
}

func TestBrokerUnknownGroupNeverTouchesDocker(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	b := broker(t, d)
	if !errors.Is(b.EnsureReady(context.Background(), "prod-db"), ErrDenied) || !errors.Is(b.Stop(context.Background(), "prod-db"), ErrDenied) {
		t.Fatal("unknown group accepted")
	}
	if len(d.inspects)+len(d.starts)+len(d.stops) != 0 {
		t.Fatal("unknown group consulted Docker")
	}
}

func TestBrokerRejectsChangedIdentityImageRoleOrEnvironment(t *testing.T) {
	mutations := []func(*ContainerInfo){
		func(v *ContainerInfo) { v.ID = strings.Repeat("c", 64) },
		func(v *ContainerInfo) { v.Image = "registry.example.invalid/xynes/auth-app:latest" },
		func(v *ContainerInfo) { v.Labels["com.xynes.environment"] = "prod" },
		func(v *ContainerInfo) { v.Labels["com.xynes.managed"] = "false" },
		func(v *ContainerInfo) { v.Labels["com.xynes.role"] = "database" },
		func(v *ContainerInfo) { v.Labels["com.docker.compose.service"] = "storage-service" },
	}
	for _, mutation := range mutations {
		d := &fixtureDocker{info: approvedContainer()}
		mutation(&d.info)
		b := broker(t, d)
		if !errors.Is(b.EnsureReady(context.Background(), "qa-auth"), ErrDenied) || !errors.Is(b.Stop(context.Background(), "qa-auth"), ErrDenied) {
			t.Fatal("changed container accepted")
		}
		if len(d.starts)+len(d.stops) != 0 {
			t.Fatal("unapproved mutation reached Docker")
		}
	}
}

func TestBrokerDoesNotStartAgainOrRestartUnhealthyRunningService(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	d.info.Running = true
	d.info.Healthy = false
	b := broker(t, d)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if !errors.Is(b.EnsureReady(ctx, "qa-auth"), ErrUnavailable) {
		t.Fatal("unhealthy service declared ready")
	}
	if len(d.starts)+len(d.stops) != 0 {
		t.Fatal("running unhealthy service restarted or stopped")
	}
}

func TestBrokerConfigurationCannotEnableCoreOrDatabaseSleep(t *testing.T) {
	for _, service := range []string{"postgres", "storage-service", "gateway", "authz-service", "cms-core"} {
		target := approvedTarget()
		target.Service = service
		if _, err := NewBroker(&fixtureDocker{}, "qa", map[string]Target{"qa-auth": target}); !errors.Is(err, ErrDenied) {
			t.Fatal("unsafe workload enabled")
		}
	}
}

func TestBrokerErrorsAreSafeAndStoppedContainerIsNoop(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	b := broker(t, d)
	if err := b.Stop(context.Background(), "qa-auth"); err != nil || len(d.stops) != 0 {
		t.Fatal("already-stopped container mutated")
	}
	d.fail = true
	if err := b.EnsureReady(context.Background(), "qa-auth"); !errors.Is(err, ErrUnavailable) || strings.Contains(err.Error(), "PRIVATE") {
		t.Fatal("Docker error leaked")
	}
	if err := b.Stop(context.Background(), "qa-auth"); !errors.Is(err, ErrUnavailable) || strings.Contains(err.Error(), "PRIVATE") {
		t.Fatal("Docker error leaked")
	}
}
