package idle

import (
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
	Version int    `json:"version"`
	Path    string `json:"path"`
	Device  uint64 `json:"device"`
	Inode   uint64 `json:"inode"`
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
	if current, err := os.Lstat(path); err == nil && current.Mode()&os.ModeSocket != 0 && os.SameFile(current, inode) {
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
		if err != nil || len(data) > 1024 || decodeRuntimeConfig(data, &record) != nil || record.Version != 1 || record.Path != path || record.Inode == 0 {
			return nil, ErrDenied
		}
	}
	existing, err := os.Lstat(path)
	if err == nil {
		socketStat, ok := existing.Sys().(*syscall.Stat_t)
		if fresh || !ok || existing.Mode()&os.ModeSocket == 0 || existing.Mode().Perm() != mode || socketStat.Uid != uid || socketStat.Gid != gid || socketStat.Nlink != 1 || record.Device != uint64(socketStat.Dev) || record.Inode != socketStat.Ino {
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
		if err != nil || !os.SameFile(existing, current) {
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
	if os.Chown(path, int(uid), int(gid)) != nil || os.Chmod(path, mode) != nil {
		return nil, ErrDenied
	}
	socketStat, ok := inode.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, ErrDenied
	}
	data, err := json.Marshal(socketRecord{Version: 1, Path: path, Device: uint64(socketStat.Dev), Inode: socketStat.Ino})
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
