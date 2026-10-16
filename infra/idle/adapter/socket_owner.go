package idle

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

type socketRecord struct {
	Version       int    `json:"version"`
	Path          string `json:"path"`
	Device        uint64 `json:"device"`
	Inode         uint64 `json:"inode"`
	ChangeSeconds int64  `json:"change_seconds"`
	ChangeNanos   int64  `json:"change_nanos"`
	ModSeconds    int64  `json:"mod_seconds"`
	ModNanos      int64  `json:"mod_nanos"`
}

// Closing admission retains the ownership lock until all handlers drain.
// Release is the runtime owner's final cleanup, never the HTTP server's Close.
type ownedListener struct {
	*net.UnixListener
	lock                   *os.File
	path                   string
	inode                  os.FileInfo
	closeOnce, releaseOnce sync.Once
	closeErr               error
}

func removeSameSocket(path string, inode os.FileInfo) {
	if current, err := os.Lstat(path); err == nil && current.Mode()&os.ModeSocket != 0 && sameSocket(current, inode) {
		_ = os.Remove(path)
	}
}
func (l *ownedListener) Close() error {
	l.closeOnce.Do(func() {
		l.closeErr = l.UnixListener.Close()
		removeSameSocket(l.path, l.inode)
	})
	return l.closeErr
}
func (l *ownedListener) Release() {
	if l == nil {
		return
	}
	l.releaseOnce.Do(func() {
		_ = l.Close()
		_ = syscall.Flock(int(l.lock.Fd()), syscall.LOCK_UN)
		_ = l.lock.Close()
	})
}

func newOwnedListener(path string, uid, gid uint32, mode os.FileMode) (_ *ownedListener, result error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || uid != uint32(os.Geteuid()) || (mode != 0600 && mode != 0660) {
		return nil, ErrDenied
	}
	parent, err := os.Lstat(filepath.Dir(path))
	if err != nil || !parent.IsDir() || parent.Mode().Perm()&0027 != 0 {
		return nil, ErrDenied
	}
	stat, ok := parent.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uid {
		return nil, ErrDenied
	}
	lockPath := path + ".lock"
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_EXCL|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	fresh := err == nil
	if errors.Is(err, os.ErrExist) {
		lock, err = os.OpenFile(lockPath, os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	}
	if err != nil {
		return nil, ErrDenied
	}
	info, err := lock.Stat()
	visible, visibleErr := os.Lstat(lockPath)
	if err != nil || visibleErr != nil || !protectedMetadata(info, uid) || info.Mode().Perm() != 0600 || !os.SameFile(info, visible) {
		lock.Close()
		return nil, ErrDenied
	}
	if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		lock.Close()
		return nil, ErrBusy
	}
	defer func() {
		if result != nil {
			if fresh {
				if current, err := os.Lstat(lockPath); err == nil && os.SameFile(info, current) {
					_ = os.Remove(lockPath)
				}
			}
			_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
			_ = lock.Close()
		}
	}()
	var record socketRecord
	if !fresh {
		data, err := io.ReadAll(io.LimitReader(lock, 1025))
		if err != nil || len(data) > 1024 || decodeRuntimeConfig(data, &record) != nil || record.Version != 2 || record.Path != path || record.Inode == 0 {
			return nil, ErrDenied
		}
	}
	existing, err := os.Lstat(path)
	if err == nil {
		socketStat, ok := existing.Sys().(*syscall.Stat_t)
		if fresh || !ok || existing.Mode()&os.ModeSocket == 0 || existing.Mode().Perm() != mode || socketStat.Uid != uid || socketStat.Gid != gid || socketStat.Nlink != 1 || record.Device != uint64(socketStat.Dev) || record.Inode != socketStat.Ino || !record.matchesChange(socketStat) || record.ModSeconds != existing.ModTime().Unix() || record.ModNanos != int64(existing.ModTime().Nanosecond()) {
			return nil, ErrDenied
		}
		conn, dialErr := net.DialTimeout("unix", path, 200*time.Millisecond)
		if dialErr == nil {
			conn.Close()
			return nil, ErrBusy
		}
		if !errors.Is(dialErr, syscall.ECONNREFUSED) {
			return nil, ErrDenied
		}
		current, err := os.Lstat(path)
		if err != nil || !sameSocket(existing, current) {
			return nil, ErrDenied
		}
		if os.Remove(path) != nil {
			return nil, ErrUnavailable
		}
	} else if !os.IsNotExist(err) {
		return nil, ErrDenied
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, ErrUnavailable
	}
	listener.SetUnlinkOnClose(false)
	inode, err := os.Lstat(path)
	if err != nil {
		listener.Close()
		return nil, ErrUnavailable
	}
	published := false
	defer func() {
		if !published {
			listener.Close()
			removeSameSocket(path, inode)
		}
	}()
	// A random, past mtime distinguishes a replacement even when the kernel
	// reuses both inode and ctime within one clock tick. Connections do not
	// change a socket's mtime. Use 50 bits at microsecond precision, supported
	// by Linux and Darwin; refuse filesystems that lose the published marker.
	// This is an ownership generation, not authentication against its own UID.
	var nonce [8]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, ErrUnavailable
	}
	stamp := time.Unix(1+int64(binary.BigEndian.Uint32(nonce[:4])%(1<<30)), int64(binary.BigEndian.Uint32(nonce[4:])%1000000)*1000)
	mutate := func(operation func() error) bool {
		err := operation()
		current, statErr := os.Lstat(path)
		if statErr != nil || !os.SameFile(inode, current) {
			return false
		}
		// Refresh even on failure: a partially successful metadata syscall must
		// not strand the socket created by this still-held owner.
		inode = current
		return err == nil
	}
	if !mutate(func() error { return os.Chown(path, int(uid), int(gid)) }) ||
		!mutate(func() error { return os.Chmod(path, mode) }) ||
		!mutate(func() error { return os.Chtimes(path, stamp, stamp) }) || !inode.ModTime().Equal(stamp) {
		return nil, ErrDenied
	}
	socketStat, ok := inode.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, ErrDenied
	}
	seconds, nanos := socketChange(socketStat)
	data, err := json.Marshal(socketRecord{Version: 2, Path: path, Device: uint64(socketStat.Dev), Inode: socketStat.Ino, ChangeSeconds: seconds, ChangeNanos: nanos, ModSeconds: inode.ModTime().Unix(), ModNanos: int64(inode.ModTime().Nanosecond())})
	if err != nil || lock.Truncate(0) != nil {
		return nil, ErrUnavailable
	}
	if _, err = lock.Seek(0, 0); err != nil {
		return nil, ErrUnavailable
	}
	if _, err = lock.Write(data); err != nil || lock.Sync() != nil {
		return nil, ErrUnavailable
	}
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return nil, ErrUnavailable
	}
	syncErr := directory.Sync()
	directory.Close()
	if syncErr != nil {
		return nil, ErrUnavailable
	}
	published = true
	return &ownedListener{UnixListener: listener, lock: lock, path: path, inode: inode}, nil
}

// Inodes can be reused after unlink. A recorded change timestamp also protects
// cleanup against an object whose ownership metadata changed after publication.
func sameSocket(left, right os.FileInfo) bool {
	if !os.SameFile(left, right) {
		return false
	}
	a, aOK := left.Sys().(*syscall.Stat_t)
	b, bOK := right.Sys().(*syscall.Stat_t)
	if !aOK || !bOK {
		return false
	}
	aSec, aNano := socketChange(a)
	bSec, bNano := socketChange(b)
	return aSec == bSec && aNano == bNano && left.ModTime().Equal(right.ModTime())
}
func (r socketRecord) matchesChange(info *syscall.Stat_t) bool {
	seconds, nanos := socketChange(info)
	return r.ChangeSeconds == seconds && r.ChangeNanos == nanos
}
