package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	idle "xynes.local/idle-adapter"
)

func run(ctx context.Context, role, path string) error {
	switch role {
	case "broker":
		var config idle.BrokerConfig
		if idle.LoadRuntimeConfig(path, &config) != nil {
			return idle.ErrDenied
		}
		return idle.RunBroker(ctx, config)
	case "proxy":
		var config idle.ProxyConfig
		if idle.LoadRuntimeConfig(path, &config) != nil {
			return idle.ErrDenied
		}
		return idle.RunProxy(ctx, config)
	default:
		return idle.ErrDenied
	}
}

func main() {
	role := flag.String("role", "", "broker or proxy")
	path := flag.String("config", "", "absolute protected configuration path")
	flag.Parse()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if flag.NArg() != 0 || run(ctx, *role, *path) != nil {
		fmt.Fprintln(os.Stderr, "idle runtime unavailable; check protected configuration and authorized runtime identity")
		os.Exit(1)
	}
}
