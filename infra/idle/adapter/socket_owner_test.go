package idle

import (
	"net"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestOwnedSocketRecoversOnlyRecordedStaleInode(t *testing.T) {
	path := filepath.Join(shortSocketDir(t), "control.sock")
	first, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
	if err != nil {
		t.Fatal("approved owned listener rejected")
	}
	// Simulate kernel cleanup after process death without normal path cleanup.
	first.UnixListener.Close()
	syscall.Flock(int(first.lock.Fd()), syscall.LOCK_UN)
	first.lock.Close()
	second, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
	if err != nil {
		t.Fatal("recorded stale socket did not recover")
	}
	defer second.Release()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal("recovered listener unavailable")
	}
	conn.Close()
}

func TestOwnedSocketLiveAuthorityAndDrainHoldDenyReplacement(t *testing.T) {
	path := filepath.Join(shortSocketDir(t), "control.sock")
	first, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Release()
	if second, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600); err != ErrBusy {
		if second != nil {
			second.Release()
		}
		t.Fatal("duplicate live authority allowed")
	}
	first.Close()
	if second, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600); err != ErrBusy {
		if second != nil {
			second.Release()
		}
		t.Fatal("closed listener lost authority before handler drain")
	}
	first.Release()
	second, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
	if err != nil {
		t.Fatal(err)
	}
	second.Release()
}

func TestOwnedSocketNeverDeletesUnknownObjectOrReplacement(t *testing.T) {
	for _, kind := range []string{"regular", "unknown-socket", "symlink", "unknown-lock"} {
		t.Run(kind, func(t *testing.T) {
			path := filepath.Join(shortSocketDir(t), "control.sock")
			switch kind {
			case "regular":
				os.WriteFile(path, []byte("preserve"), 0600)
			case "symlink":
				os.Symlink("unrelated", path)
			case "unknown-socket":
				old, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
				if err != nil {
					t.Fatal(err)
				}
				old.SetUnlinkOnClose(false)
				old.Close()
				os.Chmod(path, 0600)
			case "unknown-lock":
				os.WriteFile(path+".lock", []byte("preserve-unknown-lock"), 0600)
			}
			before, err := os.Lstat(path)
			if kind == "unknown-lock" {
				before, err = os.Lstat(path + ".lock")
			}
			if err != nil {
				t.Fatal(err)
			}
			if listener, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600); err == nil {
				listener.Release()
				t.Fatal("unknown object replaced")
			}
			after, err := os.Lstat(path)
			if kind == "unknown-lock" {
				after, err = os.Lstat(path + ".lock")
			}
			if err != nil || !os.SameFile(before, after) {
				t.Fatal("unknown inode changed")
			}
		})
	}
	path := filepath.Join(shortSocketDir(t), "control.sock")
	listener, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
	if err != nil {
		t.Fatal(err)
	}
	os.Remove(path)
	os.WriteFile(path, []byte("replacement"), 0600)
	listener.Release()
	if data, err := os.ReadFile(path); err != nil || string(data) != "replacement" {
		t.Fatal("shutdown deleted replacement object")
	}
}

func shortSocketDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "as-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func TestOwnedSocketUnlockedLiveListenerAndChangedInodeRemainUntouched(t *testing.T) {
	path := filepath.Join(shortSocketDir(t), "control.sock")
	first, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Release()
	syscall.Flock(int(first.lock.Fd()), syscall.LOCK_UN)
	first.lock.Close()
	if second, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600); err != ErrBusy {
		if second != nil {
			second.Release()
		}
		t.Fatal("live socket replaced after advisory lock loss")
	}
	first.UnixListener.Close()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	replacement, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer replacement.Close()
	os.Chmod(path, 0600)
	before, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	if second, err := newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600); err != ErrDenied {
		if second != nil {
			second.Release()
		}
		t.Fatal("foreign socket accepted by old ownership record")
	}
	after, err := os.Lstat(path)
	if err != nil || !os.SameFile(before, after) {
		t.Fatal("foreign socket changed")
	}
}

func TestOwnedSocketRefusesUnsafeDirectoryLockAndRecordWithoutDeleting(t *testing.T) {
	uid, gid := uint32(os.Geteuid()), uint32(os.Getegid())
	for _, kind := range []string{"relative", "foreign-owner", "wrong-mode", "writable-parent", "parent-symlink", "lock-directory", "lock-symlink", "lock-hardlink", "writable-lock", "oversized-record", "foreign-record"} {
		t.Run(kind, func(t *testing.T) {
			dir := shortSocketDir(t)
			path := filepath.Join(dir, "control.sock")
			owner := uid
			mode := os.FileMode(0600)
			switch kind {
			case "relative":
				path = "control.sock"
			case "foreign-owner":
				owner++
			case "wrong-mode":
				mode = 0666
			case "writable-parent":
				if err := os.Chmod(dir, 0777); err != nil {
					t.Fatal(err)
				}
			case "parent-symlink":
				link := filepath.Join(dir, "link")
				if err := os.Symlink(dir, link); err != nil {
					t.Fatal(err)
				}
				path = filepath.Join(link, "control.sock")
			case "lock-directory":
				if err := os.Mkdir(path+".lock", 0700); err != nil {
					t.Fatal(err)
				}
			case "lock-symlink":
				if err := os.WriteFile(filepath.Join(dir, "unrelated"), []byte("preserve"), 0600); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink("unrelated", path+".lock"); err != nil {
					t.Fatal(err)
				}
			case "lock-hardlink":
				source := filepath.Join(dir, "unrelated")
				if err := os.WriteFile(source, []byte("preserve"), 0600); err != nil {
					t.Fatal(err)
				}
				if err := os.Link(source, path+".lock"); err != nil {
					t.Fatal(err)
				}
			case "writable-lock":
				if err := os.WriteFile(path+".lock", []byte("preserve"), 0666); err != nil {
					t.Fatal(err)
				}
				os.Chmod(path+".lock", 0666)
			case "oversized-record":
				if err := os.WriteFile(path+".lock", make([]byte, 1025), 0600); err != nil {
					t.Fatal(err)
				}
			case "foreign-record":
				if err := os.WriteFile(path+".lock", []byte(`{"version":1,"path":"/foreign/control.sock","device":1,"inode":1}`), 0600); err != nil {
					t.Fatal(err)
				}
			}
			before, _ := os.Lstat(path + ".lock")
			if listener, err := newOwnedListener(path, owner, gid, mode); err != ErrDenied {
				if listener != nil {
					listener.Release()
				}
				t.Fatal("unsafe authority accepted")
			}
			if before != nil {
				after, err := os.Lstat(path + ".lock")
				if err != nil || !os.SameFile(before, after) {
					t.Fatal("unsafe lock object changed")
				}
			}
		})
	}
	path := filepath.Join(shortSocketDir(t), strings.Repeat("s", 150))
	if listener, err := newOwnedListener(path, uid, gid, 0600); err != ErrUnavailable {
		if listener != nil {
			listener.Release()
		}
		t.Fatal("failed socket bind misclassified")
	}
	if _, err := os.Lstat(path + ".lock"); !os.IsNotExist(err) {
		t.Fatal("failed fresh bind left ownership record")
	}
	var absent *ownedListener
	absent.Release()
}
