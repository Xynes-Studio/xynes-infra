//go:build linux

package idle

import (
	"context"
	"net"
	"syscall"
)

// Linux derives identity from the Unix connection, never an HTTP header.
func privatePeerContext(ctx context.Context, conn net.Conn) context.Context {
	unix, ok := conn.(*net.UnixConn)
	if !ok {
		return ctx
	}
	raw, err := unix.SyscallConn()
	if err != nil {
		return ctx
	}
	uid := -1
	if raw.Control(func(fd uintptr) {
		cred, e := syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
		if e == nil {
			uid = int(cred.Uid)
		}
	}) != nil || uid < 0 {
		return ctx
	}
	return context.WithValue(ctx, peerUIDKey{}, uid)
}
