package idle

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fixtureProvider struct {
	starts           atomic.Int32
	stops            atomic.Int32
	ready            chan struct{}
	stopGate         chan struct{}
	fail             bool
	stopFail         bool
	stopUndispatched bool
}

func (p *fixtureProvider) EnsureReady(ctx context.Context, _ string) error {
	p.starts.Add(1)
	if p.ready != nil {
		select {
		case <-p.ready:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if p.fail {
		return errors.New("DO-NOT-EXPOSE-private-material")
	}
	return nil
}
func (p *fixtureProvider) Stop(ctx context.Context, _ string) error {
	if p.stopUndispatched {
		return ErrControlUnavailable
	}
	p.stops.Add(1)
	if p.stopGate != nil {
		select {
		case <-p.stopGate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if p.stopFail {
		return errors.New("DO-NOT-EXPOSE-private-material")
	}
	return nil
}

func TestUnavailablePrivateControllerKeepsExistingWarmWorkUsable(t *testing.T) {
	p := &fixtureProvider{}
	m, clock := setup(t, p, 1)
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
	p.stopUndispatched = true
	clock.advance(time.Second)
	if err := m.Tick(context.Background()); err != ErrUnavailable {
		t.Fatal("controller outage not reported")
	}
	p.fail = true
	release, err = m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal("healthy warm work blocked by private controller outage")
	}
	release()
	_ = m.Tick(context.Background())
	if p.starts.Load() != 1 || p.stops.Load() != 0 {
		t.Fatal("controller outage caused lifecycle mutation")
	}
}

type fixtureClock struct {
	mu    sync.Mutex
	value time.Time
}

func (c *fixtureClock) now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.value }
func (c *fixtureClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.value = c.value.Add(d)
}

func setup(t *testing.T, p *fixtureProvider, maximum int) (*Manager, *fixtureClock) {
	t.Helper()
	c := &fixtureClock{value: time.Unix(1000, 0)}
	m, err := NewManager(p, map[string]Policy{"qa-auth": {IdleFor: time.Second, WakeTimeout: time.Second, MaxInFlight: maximum}}, c.now)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(m.Close)
	return m, c
}

func TestActiveWorkOutlivesIdleAndStartsIdleClockAfterCompletion(t *testing.T) {
	p := &fixtureProvider{}
	m, clock := setup(t, p, 10)
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	clock.advance(36 * time.Second)
	if err := m.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	if p.stops.Load() != 0 {
		t.Fatal("active request was stopped")
	}
	release()
	release()
	clock.advance(900 * time.Millisecond)
	_ = m.Tick(context.Background())
	if p.stops.Load() != 0 {
		t.Fatal("idle counted from arrival rather than completion")
	}
	clock.advance(100 * time.Millisecond)
	if err := m.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	if p.stops.Load() != 1 {
		t.Fatal("completed idle group did not stop once")
	}
	_ = m.Tick(context.Background())
	if p.stops.Load() != 1 {
		t.Fatal("already stopped group stopped again")
	}
}

func TestConcurrentColdRequestsHaveOneWake(t *testing.T) {
	p := &fixtureProvider{ready: make(chan struct{})}
	m, _ := setup(t, p, 12)
	var wg sync.WaitGroup
	errorsCh := make(chan error, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			release, err := m.Acquire(context.Background(), "qa-auth")
			if err == nil {
				release()
			}
			errorsCh <- err
		}()
	}
	deadline := time.Now().Add(time.Second)
	for p.starts.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	close(p.ready)
	wg.Wait()
	close(errorsCh)
	for err := range errorsCh {
		if err != nil {
			t.Fatal(err)
		}
	}
	if p.starts.Load() != 1 {
		t.Fatalf("wake count %d", p.starts.Load())
	}
}

func TestCancelledCallerDoesNotCancelOtherColdRequests(t *testing.T) {
	p := &fixtureProvider{ready: make(chan struct{})}
	m, _ := setup(t, p, 3)
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { _, err := m.Acquire(ctx, "qa-auth"); first <- err }()
	deadline := time.Now().Add(time.Second)
	for p.starts.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	second := make(chan error, 1)
	go func() {
		release, err := m.Acquire(context.Background(), "qa-auth")
		if err == nil {
			release()
		}
		second <- err
	}()
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	close(p.ready)
	if err := <-second; err != nil {
		t.Fatal(err)
	}
	if p.starts.Load() != 1 {
		t.Fatal("cancellation restarted wake")
	}
}

func TestAdmissionLimitIncludesWaitingColdRequests(t *testing.T) {
	p := &fixtureProvider{ready: make(chan struct{})}
	m, _ := setup(t, p, 1)
	first := make(chan error, 1)
	go func() {
		release, err := m.Acquire(context.Background(), "qa-auth")
		if err == nil {
			release()
		}
		first <- err
	}()
	deadline := time.Now().Add(time.Second)
	for p.starts.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if _, err := m.Acquire(context.Background(), "qa-auth"); !errors.Is(err, ErrBusy) {
		t.Fatalf("expected bounded admission: %v", err)
	}
	close(p.ready)
	if err := <-first; err != nil {
		t.Fatal(err)
	}
}

func TestUnknownGroupNeverCallsProvider(t *testing.T) {
	p := &fixtureProvider{}
	m, _ := setup(t, p, 1)
	if _, err := m.Acquire(context.Background(), "prod-database"); !errors.Is(err, ErrDenied) {
		t.Fatal("unknown group admitted")
	}
	if p.starts.Load() != 0 || p.stops.Load() != 0 {
		t.Fatal("provider called for unknown group")
	}
}

func TestReadinessFailureIsBoundedAndRedacted(t *testing.T) {
	for _, blocked := range []bool{false, true} {
		p := &fixtureProvider{fail: true}
		if blocked {
			p.ready = make(chan struct{})
		}
		m, _ := setup(t, p, 1)
		started := time.Now()
		if _, err := m.Acquire(context.Background(), "qa-auth"); !errors.Is(err, ErrUnavailable) || err.Error() != "workload unavailable" {
			t.Fatalf("unsafe readiness result: %v", err)
		}
		if time.Since(started) > 2*time.Second {
			t.Fatal("readiness not bounded")
		}
	}
}

func TestOwnedWarmLeasePreventsSleepUntilExpiry(t *testing.T) {
	p := &fixtureProvider{}
	m, clock := setup(t, p, 2)
	if err := m.Warm(context.Background(), "qa-auth", "deploy-42", 10*time.Second); err != nil {
		t.Fatal(err)
	}
	clock.advance(5 * time.Second)
	_ = m.Tick(context.Background())
	if p.stops.Load() != 0 {
		t.Fatal("leased deployment slept")
	}
	if err := m.ReleaseWarm("qa-auth", "other-owner"); !errors.Is(err, ErrDenied) {
		t.Fatal("foreign lease release admitted")
	}
	clock.advance(6 * time.Second)
	_ = m.Tick(context.Background())
	if p.stops.Load() != 1 {
		t.Fatal("expired warm lease did not permit sleep")
	}
}

func TestStopFailureDisablesFurtherStops(t *testing.T) {
	p := &fixtureProvider{stopFail: true}
	m, clock := setup(t, p, 1)
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
	clock.advance(time.Second)
	if err := m.Tick(context.Background()); !errors.Is(err, ErrUnavailable) {
		t.Fatal("stop failure not reported safely")
	}
	_ = m.Tick(context.Background())
	if p.stops.Load() != 1 {
		t.Fatal("continued stopping after controller fault")
	}
	release, err = m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal("warm recovery failed")
	}
	release()
}

func TestClosingControllerDoesNotStopWork(t *testing.T) {
	p := &fixtureProvider{}
	m, clock := setup(t, p, 1)
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
	m.Close()
	clock.advance(10 * time.Second)
	_ = m.Tick(context.Background())
	if p.stops.Load() != 0 {
		t.Fatal("shutdown stopped containers")
	}
}
