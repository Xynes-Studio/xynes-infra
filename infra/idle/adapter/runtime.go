package idle

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
)

func newPrivateListener(path string, gid int) (*ownedListener, error) {
	if os.Geteuid() != 0 || gid < 1 || rootParents(path) != nil {
		return nil, ErrDenied
	}
	parent, err := os.Lstat(filepath.Dir(path))
	if err != nil {
		return nil, ErrDenied
	}
	stat, ok := parent.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Gid) != gid || parent.Mode().Perm() != 0750 {
		return nil, ErrDenied
	}
	return newOwnedListener(path, 0, uint32(gid), 0660)
}

func newLeaseListener(path string) (*ownedListener, error) {
	if rootParents(filepath.Dir(path)) != nil {
		return nil, ErrDenied
	}
	info, err := os.Lstat(filepath.Dir(path))
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
		return nil, ErrDenied
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != os.Geteuid() {
		return nil, ErrDenied
	}
	return newOwnedListener(path, uint32(os.Geteuid()), uint32(os.Getegid()), 0600)
}

func serveRuntime(ctx context.Context, server *http.Server, listener net.Listener, beforeDrain func()) error {
	result := make(chan error, 1)
	go func() { result <- server.Serve(listener) }()
	select {
	case err := <-result:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return ErrUnavailable
	case <-ctx.Done():
		beforeDrain()
		drain, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		if server.Shutdown(drain) != nil {
			server.Close()
			return ErrUnavailable
		}
		<-result
		return nil
	}
}

// RunBroker is the only executable role that constructs a Docker transport.
// It listens on a root-owned Unix socket, enforces kernel peer identity, then
// verifies the stage token and exact approved image/container at each operation.
func RunBroker(ctx context.Context, config BrokerConfig) error {
	if runtime.GOOS != "linux" || config.Validate(os.Geteuid()) != nil {
		return ErrDenied
	}
	token, err := loadStageToken(config.TokenFile, config.SocketGID)
	if err != nil {
		return ErrDenied
	}
	client, err := NewUnixDockerClient(config.DockerSocket)
	if err != nil {
		return ErrDenied
	}
	broker, err := NewBroker(client, config.Environment, config.Targets)
	if err != nil {
		return ErrDenied
	}
	handler, err := NewControlHandler(broker, token)
	if err != nil {
		return ErrDenied
	}
	listener, err := newPrivateListener(config.ControlSocket, config.SocketGID)
	if err != nil {
		return err
	}
	defer listener.Release()
	server := &http.Server{Handler: peerGuard(config.ProxyUID, handler), ConnContext: privatePeerContext, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 125 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192, ErrorLog: log.New(io.Discard, "", 0)}
	return serveRuntime(ctx, server, listener, func() {})
}

// RunProxy is unprivileged and constructs only the private control client.
// No Docker transport, container identifier or lifecycle endpoint is exposed
// on its request listener. Shutdown disables stops before draining requests.
func RunProxy(ctx context.Context, config ProxyConfig) error {
	if runtime.GOOS != "linux" || config.Validate(os.Geteuid()) != nil {
		return ErrDenied
	}
	token, err := loadStageToken(config.TokenFile, stageUID(config.Environment))
	if err != nil {
		return ErrDenied
	}
	provider, err := NewUnixControlClient(config.ControlSocket, token)
	if err != nil {
		return ErrDenied
	}
	manager, err := NewManager(provider, map[string]Policy{config.Policy.Group: config.managerPolicy()}, time.Now)
	if err != nil {
		return ErrDenied
	}
	defer manager.Close()
	if rootParents(filepath.Dir(config.StateFile)) != nil {
		return ErrDenied
	}
	store, err := NewWarmStore(config.StateFile, config.Environment, config.Policy.Group, uint32(os.Geteuid()))
	if err != nil {
		return ErrDenied
	}
	if err := manager.EnablePersistence(store); err != nil && !errors.Is(err, ErrUnavailable) {
		return err
	}
	if config.StartDrained {
		if err := manager.StartDrained(config.Policy.Group); err != nil {
			return err
		}
	}
	startup, startupCancel := context.WithTimeout(ctx, 10*time.Second)
	err = manager.Reconcile(startup, config.Policy.Group)
	startupCancel()
	if err != nil {
		return err
	}
	proxy, err := NewProxy(manager, config.Policy)
	if err != nil {
		return ErrDenied
	}
	listener, err := net.Listen("tcp", config.Listen)
	if err != nil {
		return ErrUnavailable
	}
	defer listener.Close()
	warmHandler, err := NewWarmHandler(manager, config.Policy.Group, token)
	if err != nil {
		return ErrDenied
	}
	warmListener, err := newLeaseListener(config.leaseSocket())
	if err != nil {
		return err
	}
	defer warmListener.Release()
	stopTick, tickCancel := context.WithCancel(ctx)
	defer tickCancel()
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stopTick.Done():
				return
			case <-ticker.C:
				_ = manager.Tick(stopTick)
			}
		}
	}()
	server := &http.Server{Handler: ingressGuard(config.Policy, proxy), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32768, ErrorLog: log.New(io.Discard, "", 0)}
	warmServer := &http.Server{Handler: warmHandler, ConnContext: privatePeerContext, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 125 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192, ErrorLog: log.New(io.Discard, "", 0)}
	privateResult := make(chan error, 1)
	go func() {
		err := serveRuntime(stopTick, warmServer, warmListener, manager.Quiesce)
		tickCancel()
		privateResult <- err
	}()
	publicErr := serveRuntime(stopTick, server, listener, func() { tickCancel(); manager.Quiesce() })
	tickCancel()
	privateErr := <-privateResult
	if publicErr != nil {
		return publicErr
	}
	return privateErr
}
