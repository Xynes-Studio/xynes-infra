package main

import (
	"context"
	"testing"
)

func TestCommandRejectsUnknownRoleAndUnprotectedConfig(t *testing.T) {
	for _, role := range []string{"", "docker", "broker", "proxy"} {
		if run(context.Background(), role, "/not/an/approved/config") == nil {
			t.Fatal("unconfigured executable role started")
		}
	}
}
