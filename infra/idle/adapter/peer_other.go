//go:build !linux

package idle

import (
	"context"
	"net"
)

// Unsupported hosts cannot authenticate a private control peer.
func privatePeerContext(ctx context.Context, _ net.Conn) context.Context { return ctx }
