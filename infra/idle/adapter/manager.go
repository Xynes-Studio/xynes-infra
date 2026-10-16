// Package idle coordinates bounded request work and owned warm leases.
// Container authorization and HTTP forwarding belong to the narrow provider
// and proxy boundaries; this component never receives a Docker socket.
package idle

import (
	"context"
	"errors"
	"regexp"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

var (
	ErrDenied      = errors.New("workload denied")
	ErrBusy        = errors.New("workload admission limit reached")
	ErrUnavailable = errors.New("workload unavailable")
)

type Policy struct {
	IdleFor     time.Duration
	WakeTimeout time.Duration
	MaxInFlight int
}

// Provider must restrict these operations to exact approved container IDs and
// verify their environment/image/ownership. No arbitrary Docker API is exposed.
type Provider interface {
	EnsureReady(context.Context, string) error
	Stop(context.Context, string) error
}

type flight struct {
	done     chan struct{}
	err      error
	stopping bool
}
type group struct {
	mu        sync.Mutex
	policy    Policy
	active    int
	ready     bool
	flight    *flight
	completed time.Time
	warm      map[string]time.Time
	draining  string
}

type Manager struct {
	provider          Provider
	groups            map[string]*group
	now               func() time.Time
	life              context.Context
	cancel            context.CancelFunc
	stopsDisabled     atomic.Bool
	leaseMu           sync.Mutex
	store             *WarmStore
	persistenceFailed bool
}

var ownerPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$`)

func NewManager(provider Provider, policies map[string]Policy, now func() time.Time) (*Manager, error) {
	if provider == nil || now == nil || len(policies) == 0 || len(policies) > 64 {
		return nil, ErrDenied
	}
	life, cancel := context.WithCancel(context.Background())
	m := &Manager{provider: provider, groups: make(map[string]*group, len(policies)), now: now, life: life, cancel: cancel}
	for name, policy := range policies {
		if !ownerPattern.MatchString(name) || policy.IdleFor <= 0 || policy.IdleFor > 24*time.Hour || policy.WakeTimeout <= 0 || policy.WakeTimeout > 2*time.Minute || policy.MaxInFlight < 1 || policy.MaxInFlight > 256 {
			cancel()
			return nil, ErrDenied
		}
		m.groups[name] = &group{policy: policy, completed: now(), warm: make(map[string]time.Time)}
	}
	return m, nil
}

func (m *Manager) release(g *group) func() {
	var once sync.Once
	return func() { once.Do(func() { g.mu.Lock(); defer g.mu.Unlock(); g.active--; g.completed = m.now() }) }
}

func (m *Manager) wake(name string, g *group, f *flight) {
	ctx, cancel := context.WithTimeout(m.life, g.policy.WakeTimeout)
	defer cancel()
	err := m.provider.EnsureReady(ctx, name)
	g.mu.Lock()
	defer g.mu.Unlock()
	if err != nil {
		f.err = ErrUnavailable
	} else {
		g.ready = true
	}
	if g.active == 0 {
		g.completed = m.now()
	}
	g.flight = nil
	close(f.done)
}

// Acquire reserves capacity before waiting for readiness. Its idempotent
// release must be deferred until the entire forwarded response/stream ends.
// One caller's cancellation does not cancel another caller's shared wake.
func (m *Manager) Acquire(ctx context.Context, name string) (func(), error) {
	g, ok := m.groups[name]
	if !ok {
		return nil, ErrDenied
	}
	if m.life.Err() != nil {
		return nil, ErrUnavailable
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	g.mu.Lock()
	if g.draining != "" {
		g.mu.Unlock()
		return nil, ErrUnavailable
	}
	if g.active >= g.policy.MaxInFlight {
		g.mu.Unlock()
		return nil, ErrBusy
	}
	g.active++
	g.mu.Unlock()
	release := m.release(g)
	for {
		g.mu.Lock()
		if g.ready && g.flight == nil {
			g.mu.Unlock()
			return release, nil
		}
		f := g.flight
		if f == nil {
			f = &flight{done: make(chan struct{})}
			g.flight = f
			go m.wake(name, g, f)
		}
		g.mu.Unlock()
		select {
		case <-ctx.Done():
			release()
			return nil, ctx.Err()
		case <-m.life.Done():
			release()
			return nil, ErrUnavailable
		case <-f.done:
			if f.err != nil && !f.stopping {
				release()
				return nil, ErrUnavailable
			}
		}
	}
}

// Warm establishes an owned bounded lease after actual readiness. Authentication
// of the caller belongs to the private control endpoint, never this owner string.
func (m *Manager) Warm(ctx context.Context, name, owner string, ttl time.Duration) error {
	if !ownerPattern.MatchString(owner) || ttl <= 0 || ttl > 24*time.Hour {
		return ErrDenied
	}
	m.leaseMu.Lock()
	failed := m.persistenceFailed
	m.leaseMu.Unlock()
	if failed {
		return ErrUnavailable
	}
	release, err := m.Acquire(ctx, name)
	if err != nil {
		return err
	}
	defer release()
	m.leaseMu.Lock()
	defer m.leaseMu.Unlock()
	if m.persistenceFailed {
		return ErrUnavailable
	}
	g := m.groups[name]
	g.mu.Lock()
	if _, exists := g.warm[owner]; !exists && len(g.warm) >= 64 {
		g.mu.Unlock()
		return ErrBusy
	}
	g.warm[owner] = m.now().Add(ttl)
	g.mu.Unlock()
	return m.persistWarm(g)
}

func (m *Manager) ReleaseWarm(name, owner string) error {
	m.leaseMu.Lock()
	defer m.leaseMu.Unlock()
	if m.persistenceFailed {
		return ErrUnavailable
	}
	g, ok := m.groups[name]
	if !ok {
		return ErrDenied
	}
	g.mu.Lock()
	if _, exists := g.warm[owner]; !exists {
		g.mu.Unlock()
		return ErrDenied
	}
	if g.draining != "" {
		g.mu.Unlock()
		return ErrDenied
	}
	previous := g.warm[owner]
	delete(g.warm, owner)
	g.completed = m.now()
	g.mu.Unlock()
	if err := m.persistWarm(g); err != nil {
		g.mu.Lock()
		g.warm[owner] = previous
		g.mu.Unlock()
		return err
	}
	return nil
}

// Tick stops only ready, unused, unleased groups after completion-based idle.
// A stop failure disables further stopping until operator recovery; subsequent
// requests recheck readiness rather than trusting an uncertain container state.
func (m *Manager) Tick(ctx context.Context) error {
	if m.stopsDisabled.Load() || m.life.Err() != nil {
		return nil
	}
	names := make([]string, 0, len(m.groups))
	for name := range m.groups {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		m.leaseMu.Lock()
		if m.stopsDisabled.Load() || m.life.Err() != nil {
			m.leaseMu.Unlock()
			return nil
		}
		g := m.groups[name]
		g.mu.Lock()
		now := m.now()
		for owner, expiry := range g.warm {
			if !now.Before(expiry) {
				delete(g.warm, owner)
			}
		}
		if g.draining != "" || !g.ready || g.flight != nil || g.active != 0 || len(g.warm) != 0 || now.Sub(g.completed) < g.policy.IdleFor {
			g.mu.Unlock()
			m.leaseMu.Unlock()
			continue
		}
		f := &flight{done: make(chan struct{}), stopping: true}
		g.flight = f
		g.mu.Unlock()
		m.leaseMu.Unlock()
		stopCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
		err := m.provider.Stop(stopCtx, name)
		cancel()
		g.mu.Lock()
		// Only a proved pre-dispatch control failure can retain observed warm
		// state. An ambiguous, denied or dispatched stop remains fail-closed.
		if !errors.Is(err, ErrControlUnavailable) {
			g.ready = false
		}
		if err != nil {
			f.err = ErrUnavailable
			m.stopsDisabled.Store(true)
		}
		g.flight = nil
		close(f.done)
		g.mu.Unlock()
		if err != nil {
			return ErrUnavailable
		}
	}
	return nil
}

// Close cancels outstanding wake waits and disables new stops. It deliberately
// does not stop any container as part of controller shutdown.
func (m *Manager) Close() {
	m.Quiesce()
	m.leaseMu.Lock()
	defer m.leaseMu.Unlock()
	if m.store != nil {
		m.store.close()
	}
}

// Quiesce rejects new work/stops while retaining the persistence writer lock.
// Runtime must call Close only after both HTTP servers finish their drain.
func (m *Manager) Quiesce() { m.stopsDisabled.Store(true); m.cancel() }

type Status struct {
	Phase         string `json:"phase"`
	Active        int    `json:"active"`
	StopsDisabled bool   `json:"stops_disabled"`
	Draining      bool   `json:"draining"`
}

// Snapshot reports controller-observed state. It does not attest application
// readiness or replace a provider health check after container/image changes.
func (m *Manager) Snapshot(name string) (Status, error) {
	g, ok := m.groups[name]
	if !ok {
		return Status{}, ErrDenied
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	phase := "asleep"
	if g.ready {
		phase = "awake_last_seen"
	}
	if g.flight != nil {
		if g.flight.stopping {
			phase = "stopping"
		} else {
			phase = "starting"
		}
	}
	return Status{Phase: phase, Active: g.active, StopsDisabled: m.stopsDisabled.Load(), Draining: g.draining != ""}, nil
}

// Invalidate makes the next request consult provider readiness after a transport
// failure. It neither stops nor restarts a workload with other active leases.
func (m *Manager) Invalidate(name string) {
	g, ok := m.groups[name]
	if !ok {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	g.ready = false
}

// Reconcile observes a protected exact target on process restart without
// starting a stopped workload. Unknown state refuses admission at startup.
func (m *Manager) Reconcile(ctx context.Context, name string) error {
	g, ok := m.groups[name]
	if !ok {
		return ErrDenied
	}
	observer, ok := m.provider.(interface {
		Observe(context.Context, string) (bool, error)
	})
	if !ok {
		return ErrDenied
	}
	ready, err := observer.Observe(ctx, name)
	if err != nil {
		return ErrUnavailable
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.active != 0 || g.flight != nil {
		return ErrBusy
	}
	g.ready = ready
	g.completed = m.now()
	return nil
}

// StartDrained is selected only by protected root configuration during a
// generation switch. Existing durable warm holds must authorize the resume.
func (m *Manager) StartDrained(name string) error {
	g, ok := m.groups[name]
	if !ok {
		return ErrDenied
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.active != 0 || g.flight != nil {
		return ErrBusy
	}
	g.draining = "startup"
	return nil
}

// Drain closes admission before waiting for every complete response/stream.
// A timeout retains closed admission and the durable deployment warm lease.
func (m *Manager) Drain(ctx context.Context, name, owner string) error {
	g, ok := m.groups[name]
	if !ok || !ownerPattern.MatchString(owner) {
		return ErrDenied
	}
	g.mu.Lock()
	expiry, owned := g.warm[owner]
	if !owned || !m.now().Before(expiry) || (g.draining != "" && g.draining != "startup" && g.draining != owner) {
		g.mu.Unlock()
		return ErrDenied
	}
	g.draining = owner
	g.mu.Unlock()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		g.mu.Lock()
		idle := g.active == 0 && g.flight == nil
		g.mu.Unlock()
		if idle {
			return nil
		}
		select {
		case <-ctx.Done():
			return ErrUnavailable
		case <-m.life.Done():
			return ErrUnavailable
		case <-ticker.C:
		}
	}
}

func (m *Manager) Resume(name, owner string) error {
	g, ok := m.groups[name]
	if !ok || !ownerPattern.MatchString(owner) {
		return ErrDenied
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	expiry, owned := g.warm[owner]
	if !owned || !m.now().Before(expiry) {
		return ErrDenied
	}
	if g.draining == "" {
		return nil
	}
	if (g.draining != owner && g.draining != "startup") || g.active != 0 || g.flight != nil {
		return ErrDenied
	}
	g.draining = ""
	g.completed = m.now()
	return nil
}

func (m *Manager) Endpoint(ctx context.Context, name string) (string, error) {
	if _, ok := m.groups[name]; !ok {
		return "", ErrDenied
	}
	resolver, ok := m.provider.(interface {
		Endpoint(context.Context, string) (string, error)
	})
	if !ok {
		return "", ErrDenied
	}
	return resolver.Endpoint(ctx, name)
}
