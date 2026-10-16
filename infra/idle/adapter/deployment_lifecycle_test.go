package idle

import (
	"context"
	"errors"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type observedProvider struct {
	fixtureProvider
	observed         bool
	observationError error
}

func (p *observedProvider) Observe(context.Context, string) (bool, error) {
	return p.observed, p.observationError
}
func TestRestartReconcilesWarmContainerWithoutStartingColdContainer(t *testing.T) {
	for _, running := range []bool{false, true} {
		p := &observedProvider{observed: running}
		clock := &fixtureClock{value: time.Unix(1000, 0)}
		m, err := NewManager(p, map[string]Policy{"qa-auth": {IdleFor: time.Second, WakeTimeout: time.Second, MaxInFlight: 4}}, clock.now)
		if err != nil {
			t.Fatal(err)
		}
		defer m.Close()
		if err := m.Reconcile(context.Background(), "qa-auth"); err != nil {
			t.Fatal(err)
		}
		clock.advance(2 * time.Second)
		if err := m.Tick(context.Background()); err != nil {
			t.Fatal(err)
		}
		if p.starts.Load() != 0 || (p.stops.Load() == 1) != running {
			t.Fatal("restart woke cold workload or stranded running idle workload")
		}
	}
}
func TestDeploymentDrainWaitsForActiveStreamAndRejectsNewWork(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	if err := m.Warm(context.Background(), "qa-auth", "deploy-1", time.Hour); err != nil {
		t.Fatal(err)
	}
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err = m.Drain(ctx, "qa-auth", "deploy-1"); err == nil {
		t.Fatal("active stream was not drained")
	}
	if _, err = m.Acquire(context.Background(), "qa-auth"); !errors.Is(err, ErrUnavailable) {
		t.Fatal("drained proxy admitted new work")
	}
	if err = m.Resume("qa-auth", "foreign"); err == nil {
		t.Fatal("foreign resume accepted")
	}
	release()
	if err = m.Drain(context.Background(), "qa-auth", "deploy-1"); err != nil {
		t.Fatal(err)
	}
	if err = m.ReleaseWarm("qa-auth", "deploy-1"); err == nil {
		t.Fatal("drain owner released before validated resume")
	}
	if err = m.Resume("qa-auth", "deploy-1"); err != nil {
		t.Fatal(err)
	}
	release, err = m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
}
func TestPrivateDeploymentEndpointsRequireRootOwnedWarmHold(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	token := strings.Repeat("f", 64)
	h, err := NewWarmHandler(m, "qa-auth", token)
	if err != nil {
		t.Fatal(err)
	}
	call := func(path, owner string, uid int) int {
		r := httptest.NewRequest("POST", path, strings.NewReader(`{"group":"qa-auth","owner":"`+owner+`"}`))
		r.Header.Set("Authorization", "Bearer "+token)
		r = r.WithContext(context.WithValue(r.Context(), peerUIDKey{}, uid))
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w.Code
	}
	if call("/drain", "deploy-1", 0) != 403 {
		t.Fatal("unowned drain accepted")
	}
	if err = m.Warm(context.Background(), "qa-auth", "deploy-1", time.Hour); err != nil {
		t.Fatal(err)
	}
	if call("/drain", "deploy-1", 12002) != 403 {
		t.Fatal("nonroot drain accepted")
	}
	if call("/drain", "deploy-1", 0) != 204 || call("/resume", "deploy-1", 0) != 204 {
		t.Fatal("root deployment flow refused")
	}
}
func TestHostProxyConfigAllowsOnlyPrivateBridgeAndStagePorts(t *testing.T) {
	p := validProxyConfig()
	p.HostNetwork = true
	p.Policy.DynamicTarget = true
	p.StartDrained = true
	p.Listen = "172.21.0.1:18081"
	p.Policy.Target = "http://172.19.0.4:3100"
	if p.Validate(12002) != nil {
		t.Fatal("reviewed host bridge binding refused")
	}
	for _, listen := range []string{"0.0.0.0:18081", "84.247.176.134:18081", "172.21.0.1:18281", "127.0.0.1:18081"} {
		bad := p
		bad.Listen = listen
		if bad.Validate(12002) == nil {
			t.Fatal("unsafe listener accepted")
		}
	}
	for _, target := range []string{"http://84.247.176.134:3100", "http://172.20.0.4:3000", "http://172.20.0.4:3100/private", "http://auth_app_qa:3100"} {
		bad := p
		bad.Policy.Target = target
		if bad.Validate(12002) == nil {
			t.Fatal("unsafe upstream accepted")
		}
	}
}
func TestPassiveControlObservationChecksExactTargetWithoutLifecycleMutation(t *testing.T) {
	d := &fixtureDocker{info: approvedContainer()}
	h, err := NewControlHandler(broker(t, d), strings.Repeat("f", 64))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(h)
	defer server.Close()
	client := newControlHTTPClient(server.URL, strings.Repeat("f", 64), server.Client())
	ready, err := client.Observe(context.Background(), "qa-auth")
	if err != nil || ready || len(d.starts) != 0 || len(d.stops) != 0 {
		t.Fatal("passive observation mutated cold workload")
	}
	d.info.Running = true
	ready, err = client.Observe(context.Background(), "qa-auth")
	if err != nil || !ready {
		t.Fatal("running readiness not observed")
	}
	d.info.ID = strings.Repeat("c", 64)
	if _, err = client.Observe(context.Background(), "qa-auth"); err == nil {
		t.Fatal("replaced CID adopted without root authorization")
	}
}
func TestRestartedDrainedProxyRequiresOriginalWarmHoldToResume(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 8)
	if err := m.StartDrained("qa-auth"); err != nil {
		t.Fatal(err)
	}
	if err := m.Resume("qa-auth", "deploy-1"); err == nil {
		t.Fatal("unowned startup resume accepted")
	}
	g := m.groups["qa-auth"]
	g.mu.Lock()
	g.warm["deploy-1"] = m.now().Add(time.Hour)
	g.mu.Unlock()
	if _, err := m.Acquire(context.Background(), "qa-auth"); err != ErrUnavailable {
		t.Fatal("new generation opened before root validation")
	}
	if err := m.Resume("qa-auth", "deploy-1"); err != nil {
		t.Fatal(err)
	}
}

func TestDurableHoldRetainsOriginalExpiryAcrossDrainedGenerationRestart(t *testing.T) {
	path := filepath.Join(privateLeaseDir(t), "warm.json")
	clock := &fixtureClock{value: time.Unix(1000, 0)}
	provider := &fixtureProvider{}
	old := persistentFixture(t, path, provider, clock)
	if old.Warm(context.Background(), "qa-auth", "deploy-42", time.Hour) != nil {
		t.Fatal("warm refused")
	}
	expiry := old.groups["qa-auth"].warm["deploy-42"]
	if old.Drain(context.Background(), "qa-auth", "deploy-42") != nil {
		t.Fatal("drain refused")
	}
	old.Close()
	clock.advance(time.Minute)
	next := persistentFixture(t, path, provider, clock)
	if next.StartDrained("qa-auth") != nil {
		t.Fatal("startup drain refused")
	}
	if next.groups["qa-auth"].warm["deploy-42"] != expiry {
		t.Fatal("generation restart altered hold expiry")
	}
	if next.Resume("qa-auth", "deploy-42") != nil || next.ReleaseWarm("qa-auth", "deploy-42") != nil {
		t.Fatal("validated generation could not resume/release durable hold")
	}
}

func TestValidatedResumeIsIdempotentWhileNewWorkRuns(t *testing.T) {
	provider := &fixtureProvider{}
	m, _ := setup(t, provider, 8)
	if m.Warm(context.Background(), "qa-auth", "deploy-42", time.Hour) != nil || m.Drain(context.Background(), "qa-auth", "deploy-42") != nil || m.Resume("qa-auth", "deploy-42") != nil {
		t.Fatal("resume refused")
	}
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if m.Resume("qa-auth", "deploy-42") != nil {
		t.Fatal("idempotent resume disturbed live generation")
	}
}
