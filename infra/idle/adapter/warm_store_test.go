package idle

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func privateLeaseDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	return dir
}

func persistentFixture(t *testing.T, path string, p *fixtureProvider, c *fixtureClock) *Manager {
	t.Helper()
	m, err := NewManager(p, map[string]Policy{"qa-auth": {IdleFor: time.Second, WakeTimeout: time.Second, MaxInFlight: 32}}, c.now)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(m.Close)
	store, err := NewWarmStore(path, "qa", "qa-auth", uint32(os.Geteuid()))
	if err != nil {
		t.Fatal(err)
	}
	if err := m.EnablePersistence(store); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestOwnedWarmLeaseSurvivesRestartAndReleaseIsDurable(t *testing.T) {
	path := filepath.Join(privateLeaseDir(t), "warm.json")
	c := &fixtureClock{value: time.Unix(1000, 0)}
	p := &fixtureProvider{}
	m := persistentFixture(t, path, p, c)
	if m.Warm(context.Background(), "qa-auth", "deploy-release-1", time.Hour) != nil {
		t.Fatal("warm failed")
	}
	m.Close()
	m = persistentFixture(t, path, p, c)
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
	c.advance(time.Minute)
	if m.Tick(context.Background()) != nil || p.stops.Load() != 0 {
		t.Fatal("restart lost deployment hold")
	}
	if m.ReleaseWarm("qa-auth", "foreign-owner") != ErrDenied {
		t.Fatal("foreign owner released hold")
	}
	if m.ReleaseWarm("qa-auth", "deploy-release-1") != nil {
		t.Fatal("durable release failed")
	}
	m.Close()
	m = persistentFixture(t, path, p, c)
	release, err = m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
	c.advance(time.Second)
	if m.Tick(context.Background()) != nil || p.stops.Load() != 1 {
		t.Fatal("released hold restored after restart")
	}
}

func TestMalformedOrForeignWarmStateDisablesStopsButKeepsRouting(t *testing.T) {
	for _, data := range []string{
		`{"version":1,"environment":"prod","group":"prod-auth","leases":[]}`,
		`{"version":1,"environment":"qa","group":"qa-auth","leases":[],"token":"PRIVATE"}`,
		`{"version":1,"environment":"qa","group":"qa-auth","leases":[{"owner":"deploy","until":999999999999999999}]}`,
		`PRIVATE-invalid-state`,
	} {
		t.Run(fmt.Sprintf("input-%d", len(data)), func(t *testing.T) {
			path := filepath.Join(privateLeaseDir(t), "warm.json")
			if os.WriteFile(path, []byte(data), 0600) != nil {
				t.Fatal("fixture write failed")
			}
			p := &fixtureProvider{}
			m, c := setup(t, p, 4)
			store, err := NewWarmStore(path, "qa", "qa-auth", uint32(os.Geteuid()))
			if err != nil {
				t.Fatal(err)
			}
			if m.EnablePersistence(store) != ErrUnavailable {
				t.Fatal("unsafe state accepted")
			}
			if m.Warm(context.Background(), "qa-auth", "deploy", time.Hour) != ErrUnavailable || p.starts.Load() != 0 {
				t.Fatal("invalid persistence caused a warm start")
			}
			release, err := m.Acquire(context.Background(), "qa-auth")
			if err != nil {
				t.Fatal("degraded persistence blocked ready routing")
			}
			release()
			c.advance(time.Hour)
			m.Tick(context.Background())
			if p.stops.Load() != 0 {
				t.Fatal("unsafe persistence allowed stop")
			}
			if m.Warm(context.Background(), "qa-auth", "deploy", time.Hour) != ErrUnavailable {
				t.Fatal("unsafe persistence granted lease")
			}
			if b, _ := os.ReadFile(path); string(b) != data {
				t.Fatal("untrusted state overwritten")
			}
		})
	}
}

func TestWarmWriteFailureDisablesStopsAndPreservesUnrelatedFile(t *testing.T) {
	dir := privateLeaseDir(t)
	path := filepath.Join(dir, "warm.json")
	p := &fixtureProvider{}
	c := &fixtureClock{value: time.Unix(1000, 0)}
	m := persistentFixture(t, path, p, c)
	other := filepath.Join(dir, "unrelated")
	if os.WriteFile(other, []byte("preserve"), 0600) != nil || os.Remove(path) != nil || os.Symlink(other, path) != nil {
		t.Fatal("fixture setup failed")
	}
	if m.Warm(context.Background(), "qa-auth", "deploy", time.Hour) != ErrUnavailable {
		t.Fatal("uncommitted lease acknowledged")
	}
	c.advance(2 * time.Hour)
	if m.Tick(context.Background()) != nil || p.stops.Load() != 0 {
		t.Fatal("persistence fault allowed lifecycle stop")
	}
	if b, _ := os.ReadFile(other); string(b) != "preserve" {
		t.Fatal("foreign file changed")
	}
}

func TestMissingPreviouslyInitializedWarmStateNeverLosesHoldSilently(t *testing.T) {
	path := filepath.Join(privateLeaseDir(t), "warm.json")
	c := &fixtureClock{value: time.Unix(1000, 0)}
	p := &fixtureProvider{}
	m := persistentFixture(t, path, p, c)
	if m.Warm(context.Background(), "qa-auth", "deploy", time.Hour) != nil {
		t.Fatal("warm failed")
	}
	m.Close()
	if os.Remove(path) != nil {
		t.Fatal("fixture remove failed")
	}
	m, _ = setup(t, p, 4)
	store, _ := NewWarmStore(path, "qa", "qa-auth", uint32(os.Geteuid()))
	if m.EnablePersistence(store) != ErrUnavailable {
		t.Fatal("missing prior state silently reset holds")
	}
}

func TestQuiescingRetainsWriterLockUntilActiveWorkDrains(t *testing.T) {
	path := filepath.Join(privateLeaseDir(t), "warm.json")
	c := &fixtureClock{value: time.Unix(1000, 0)}
	p := &fixtureProvider{}
	m := persistentFixture(t, path, p, c)
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	m.Quiesce()
	other, _ := setup(t, p, 4)
	store, _ := NewWarmStore(path, "qa", "qa-auth", uint32(os.Geteuid()))
	if other.EnablePersistence(store) != ErrBusy {
		t.Fatal("replacement controller acquired state while old work active")
	}
	if _, err := m.Acquire(context.Background(), "qa-auth"); err != ErrUnavailable {
		t.Fatal("quiescing controller admitted new work")
	}
	c.advance(time.Hour)
	m.Tick(context.Background())
	if p.stops.Load() != 0 {
		t.Fatal("quiescing controller stopped active work")
	}
	release()
	m.Close()
	if other.EnablePersistence(store) != nil {
		t.Fatal("drained controller did not release writer lock")
	}
}

func TestConcurrentOwnedWarmHoldsAndSingleWriterLock(t *testing.T) {
	path := filepath.Join(privateLeaseDir(t), "warm.json")
	c := &fixtureClock{value: time.Unix(1000, 0)}
	p := &fixtureProvider{}
	m := persistentFixture(t, path, p, c)
	second, _ := NewWarmStore(path, "qa", "qa-auth", uint32(os.Geteuid()))
	other, _ := setup(t, p, 4)
	if other.EnablePersistence(second) != ErrBusy {
		t.Fatal("multiple controllers acquired same state")
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if err := m.Warm(context.Background(), "qa-auth", fmt.Sprintf("owner-%02d", i), time.Hour); err != nil {
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	m.Close()
	m = persistentFixture(t, path, p, c)
	for i := 0; i < 20; i++ {
		if m.ReleaseWarm("qa-auth", fmt.Sprintf("owner-%02d", i)) != nil {
			t.Fatal("concurrent lease lost")
		}
	}
}

func TestWarmStoreRejectsUnsafeMetadataAndExpiredStateDoesNotRevive(t *testing.T) {
	dir := privateLeaseDir(t)
	path := filepath.Join(dir, "warm.json")
	if _, err := NewWarmStore(path, "qa", "prod-auth", uint32(os.Geteuid())); err == nil {
		t.Fatal("foreign group accepted")
	}
	if os.Chmod(dir, 0770) != nil {
		t.Fatal("fixture chmod failed")
	}
	if _, err := NewWarmStore(path, "qa", "qa-auth", uint32(os.Geteuid())); err == nil {
		t.Fatal("writable state directory accepted")
	}
	os.Chmod(dir, 0700)
	state := `{"version":1,"environment":"qa","group":"qa-auth","leases":[{"owner":"old-deploy","until":1001000000000}]}`
	if os.WriteFile(path, []byte(state), 0600) != nil {
		t.Fatal("fixture write failed")
	}
	c := &fixtureClock{value: time.Unix(1002, 0)}
	p := &fixtureProvider{}
	m := persistentFixture(t, path, p, c)
	if m.ReleaseWarm("qa-auth", "old-deploy") != ErrDenied {
		t.Fatal("expired lease revived")
	}
	release, err := m.Acquire(context.Background(), "qa-auth")
	if err != nil {
		t.Fatal(err)
	}
	release()
	c.advance(time.Second)
	if m.Tick(context.Background()) != nil || p.stops.Load() != 1 {
		t.Fatal("expired state prevents eligible idle stop")
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0600 {
		t.Fatal("state permissions changed")
	}
}
