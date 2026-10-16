package idle

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

type warmRequest struct {
	Group      string `json:"group"`
	Owner      string `json:"owner"`
	TTLSeconds int    `json:"ttl_seconds,omitempty"`
}

// This endpoint is private Unix IPC for the root deploy coordinator. Public
// ingress must never attach it; kernel root peer and stage token are both needed.
func NewWarmHandler(manager *Manager, group, token string) (http.Handler, error) {
	if manager == nil || !tokenPattern.MatchString(token) {
		return nil, ErrDenied
	}
	if _, err := manager.Snapshot(group); err != nil {
		return nil, ErrDenied
	}
	hash := sha256.Sum256([]byte(token))
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		actual := sha256.Sum256([]byte(strings.TrimPrefix(auth, "Bearer ")))
		if !strings.HasPrefix(auth, "Bearer ") || subtle.ConstantTimeCompare(actual[:], hash[:]) != 1 {
			w.WriteHeader(401)
			return
		}
		if r.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		if (r.URL.Path != "/warm" && r.URL.Path != "/release" && r.URL.Path != "/drain" && r.URL.Path != "/resume") || r.URL.RawQuery != "" {
			w.WriteHeader(404)
			return
		}
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
		decoder.DisallowUnknownFields()
		var message warmRequest
		if decoder.Decode(&message) != nil || decoder.Decode(&struct{}{}) != io.EOF {
			w.WriteHeader(400)
			return
		}
		if message.Group != group || !ownerPattern.MatchString(message.Owner) {
			w.WriteHeader(403)
			return
		}
		var err error
		if r.URL.Path == "/warm" {
			if message.TTLSeconds < 1 || message.TTLSeconds > 86400 {
				w.WriteHeader(403)
				return
			}
			err = manager.Warm(r.Context(), group, message.Owner, time.Duration(message.TTLSeconds)*time.Second)
		} else {
			if message.TTLSeconds != 0 {
				w.WriteHeader(400)
				return
			}
			switch r.URL.Path {
			case "/release":
				err = manager.ReleaseWarm(group, message.Owner)
			case "/drain":
				err = manager.Drain(r.Context(), group, message.Owner)
			case "/resume":
				err = manager.Resume(group, message.Owner)
			}
		}
		if errors.Is(err, ErrDenied) {
			w.WriteHeader(403)
			return
		}
		if err != nil {
			unavailable(w)
			return
		}
		w.WriteHeader(204)
	})
	return peerGuard(0, handler), nil
}
