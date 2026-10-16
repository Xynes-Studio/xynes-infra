package idle

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"syscall"
	"time"
)

type WarmStore struct {
	path, stage, group string
	owner              uint32
	lockFile           *os.File
	reusedLock         bool
}
type warmRecord struct {
	Owner string `json:"owner"`
	Until int64  `json:"until"` // Unix nanoseconds retain the original expiry.
}
type warmDocument struct {
	Version     int          `json:"version"`
	Environment string       `json:"environment"`
	Group       string       `json:"group"`
	Leases      []warmRecord `json:"leases"`
}

func NewWarmStore(path, stage, group string, owner uint32) (*WarmStore, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || filepath.Base(path) != "warm.json" || stageUID(stage) < 0 || (group != stage+"-auth" && group != stage+"-cms") {
		return nil, ErrDenied
	}
	s := &WarmStore{path: path, stage: stage, group: group, owner: owner}
	if s.directorySafe() != nil {
		return nil, ErrDenied
	}
	return s, nil
}

func (s *WarmStore) directorySafe() error {
	info, err := os.Lstat(filepath.Dir(s.path))
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
		return ErrDenied
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != s.owner {
		return ErrDenied
	}
	return nil
}

func (s *WarmStore) acquireLock() error {
	if s.directorySafe() != nil || s.lockFile != nil {
		return ErrDenied
	}
	path := filepath.Join(filepath.Dir(s.path), "warm.lock")
	if _, err := os.Lstat(path); err == nil {
		s.reusedLock = true
	} else if !os.IsNotExist(err) {
		return ErrDenied
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return ErrDenied
	}
	info, err := file.Stat()
	if err != nil || !protectedMetadata(info, s.owner) || info.Mode().Perm() != 0600 {
		file.Close()
		return ErrDenied
	}
	if syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		file.Close()
		return ErrBusy
	}
	s.lockFile = file
	return nil
}

func (s *WarmStore) close() {
	if s.lockFile != nil {
		_ = syscall.Flock(int(s.lockFile.Fd()), syscall.LOCK_UN)
		_ = s.lockFile.Close()
		s.lockFile = nil
	}
}

func (s *WarmStore) load(now time.Time) (map[string]time.Time, error) {
	leases := map[string]time.Time{}
	if s.directorySafe() != nil {
		return nil, ErrDenied
	}
	info, err := os.Lstat(s.path)
	if os.IsNotExist(err) {
		if s.reusedLock {
			return nil, ErrDenied
		}
		return leases, nil
	}
	if err != nil || info.Mode().Perm() != 0600 {
		return nil, ErrDenied
	}
	data, err := readOwnedFile(s.path, s.owner, 16384)
	if err != nil {
		return nil, ErrDenied
	}
	var document warmDocument
	if decodeRuntimeConfig(data, &document) != nil || document.Version != 1 || document.Environment != s.stage || document.Group != s.group || len(document.Leases) > 64 || document.Leases == nil {
		return nil, ErrDenied
	}
	seen := map[string]bool{}
	for _, record := range document.Leases {
		expiry := time.Unix(0, record.Until)
		if !ownerPattern.MatchString(record.Owner) || seen[record.Owner] || record.Until <= 0 || expiry.After(now.Add(24*time.Hour)) {
			return nil, ErrDenied
		}
		seen[record.Owner] = true
		if now.Before(expiry) {
			leases[record.Owner] = expiry
		}
	}
	return leases, nil
}

func (s *WarmStore) save(leases map[string]time.Time) error {
	if s.lockFile == nil || s.directorySafe() != nil || len(leases) > 64 {
		return ErrDenied
	}
	if info, err := os.Lstat(s.path); err == nil {
		if !protectedMetadata(info, s.owner) || info.Mode().Perm() != 0600 {
			return ErrDenied
		}
	} else if !os.IsNotExist(err) {
		return ErrDenied
	}
	document := warmDocument{Version: 1, Environment: s.stage, Group: s.group, Leases: []warmRecord{}}
	owners := make([]string, 0, len(leases))
	for owner := range leases {
		if !ownerPattern.MatchString(owner) {
			return ErrDenied
		}
		owners = append(owners, owner)
	}
	sort.Strings(owners)
	for _, owner := range owners {
		document.Leases = append(document.Leases, warmRecord{Owner: owner, Until: leases[owner].UnixNano()})
	}
	data, err := json.Marshal(document)
	if err != nil || len(data) > 16384 {
		return ErrDenied
	}
	file, err := os.CreateTemp(filepath.Dir(s.path), ".warm-write-*")
	if err != nil {
		return ErrUnavailable
	}
	defer os.Remove(file.Name()) // Only the temporary inode created by this call.
	if _, err = file.Write(data); err != nil {
		file.Close()
		return ErrUnavailable
	}
	if file.Sync() != nil {
		file.Close()
		return ErrUnavailable
	}
	if file.Close() != nil || os.Rename(file.Name(), s.path) != nil {
		return ErrUnavailable
	}
	directory, err := os.Open(filepath.Dir(s.path))
	if err != nil {
		return ErrUnavailable
	}
	defer directory.Close()
	if directory.Sync() != nil {
		return ErrUnavailable
	}
	return nil
}

// EnablePersistence is called once before request/timer admission. Invalid
// state disables all stops and new warm grants; normal routing can continue.
func (m *Manager) EnablePersistence(store *WarmStore) error {
	m.leaseMu.Lock()
	defer m.leaseMu.Unlock()
	if store == nil || m.store != nil || len(m.groups) != 1 {
		return ErrDenied
	}
	g, ok := m.groups[store.group]
	if !ok {
		return ErrDenied
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.active != 0 || g.flight != nil || g.ready || len(g.warm) != 0 {
		return ErrDenied
	}
	if err := store.acquireLock(); err != nil {
		return err
	}
	m.store = store
	leases, err := store.load(m.now())
	if err != nil {
		m.persistenceFailed = true
		m.stopsDisabled.Store(true)
		return ErrUnavailable
	}
	g.warm = leases
	if store.save(leases) != nil {
		m.persistenceFailed = true
		m.stopsDisabled.Store(true)
		return ErrUnavailable
	}
	return nil
}

func (m *Manager) persistWarm(g *group) error {
	if m.store == nil {
		return nil
	}
	g.mu.Lock()
	leases := make(map[string]time.Time, len(g.warm))
	for owner, expiry := range g.warm {
		leases[owner] = expiry
	}
	g.mu.Unlock()
	if m.store.save(leases) != nil {
		m.persistenceFailed = true
		m.stopsDisabled.Store(true)
		return ErrUnavailable
	}
	return nil
}
