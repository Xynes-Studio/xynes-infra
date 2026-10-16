//go:build linux

package idle

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestPrivateUnixSocketPermissionsAndKernelIdentity(t *testing.T) {
	if os.Geteuid() != 0 || os.Getenv("XYNES_IDLE_UNIX_FIXTURE") != "1" {
		t.Skip("requires disposable Linux root fixture")
	}
	dir := "/run/xynes/idle/qa"
	if err := os.MkdirAll(dir, 0750); err != nil {
		t.Fatal(err)
	}
	if err := os.Chown(dir, 0, 12002); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(dir, "control.sock")
	if err := os.WriteFile(socket, []byte("preserve-existing-object"), 0600); err != nil {
		t.Fatal(err)
	}
	if listener, err := newPrivateListener(socket, 12002); err == nil {
		listener.Release()
		t.Fatal("existing path replaced")
	}
	if b, _ := os.ReadFile(socket); string(b) != "preserve-existing-object" {
		t.Fatal("existing path changed")
	}
	if err := os.Remove(socket); err != nil {
		t.Fatal(err)
	}
	listener, err := newPrivateListener(socket, 12002)
	if err != nil {
		t.Fatal("protected Unix listener rejected")
	}
	defer listener.Release()
	info, err := os.Lstat(socket)
	if err != nil {
		t.Fatal(err)
	}
	stat := info.Sys().(*syscall.Stat_t)
	if info.Mode()&os.ModeSocket == 0 || info.Mode().Perm() != 0660 || stat.Uid != 0 || stat.Gid != 12002 {
		t.Fatal("unsafe socket permissions")
	}
	for _, uid := range []int{0, 12002} {
		h := peerGuard(uid, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }))
		server := &http.Server{Handler: h, ConnContext: privatePeerContext}
		go server.Serve(listener)
		client, err := unixHTTPClient(socket)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.Get("http://control/ready")
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if uid == 0 && response.StatusCode != 204 {
			t.Fatal("actual local peer credential not recognized")
		}
		if uid == 12002 && response.StatusCode != 403 {
			t.Fatal("actual foreign root peer accepted by proxy-only control")
		}
		server.Shutdown(context.Background())
		listener.Release()
		if uid == 0 {
			listener, err = newPrivateListener(socket, 12002)
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Release()
		}
	}
}
