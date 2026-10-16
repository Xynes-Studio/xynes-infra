package idle

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptrace"
	"regexp"
	"strings"
	"sync/atomic"
)

var ErrControlUnavailable = errors.New("lifecycle control unavailable")
var tokenPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type controlHandler struct {
	broker    *Broker
	tokenHash [32]byte
}
type controlRequest struct {
	Group string `json:"group"`
}

// NewControlHandler is for a private Unix listener only. Production wiring must
// additionally enforce root-owned socket/config permissions and peer identity.
func NewControlHandler(broker *Broker, token string) (http.Handler, error) {
	if broker == nil || !tokenPattern.MatchString(token) {
		return nil, ErrDenied
	}
	return &controlHandler{broker: broker, tokenHash: sha256.Sum256([]byte(token))}, nil
}

func (h *controlHandler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	provided := strings.TrimPrefix(request.Header.Get("Authorization"), "Bearer ")
	actual := sha256.Sum256([]byte(provided))
	if !strings.HasPrefix(request.Header.Get("Authorization"), "Bearer ") || subtle.ConstantTimeCompare(actual[:], h.tokenHash[:]) != 1 {
		writer.WriteHeader(401)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	if request.URL.Path == "/ready" && request.Method == "GET" && request.URL.RawQuery == "" {
		_, _ = writer.Write([]byte(`{"scope":"control","ready":true}`))
		return
	}
	if request.Method != "POST" {
		writer.WriteHeader(405)
		return
	}
	if (request.URL.Path != "/start" && request.URL.Path != "/stop" && request.URL.Path != "/observe" && request.URL.Path != "/endpoint") || request.URL.RawQuery != "" {
		writer.WriteHeader(404)
		return
	}
	decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1024))
	decoder.DisallowUnknownFields()
	var message controlRequest
	if decoder.Decode(&message) != nil || decoder.Decode(&struct{}{}) != io.EOF {
		writer.WriteHeader(400)
		return
	}

	if request.URL.Path == "/endpoint" {
		endpoint, err := h.broker.Endpoint(request.Context(), message.Group)
		if errors.Is(err, ErrDenied) {
			writer.WriteHeader(403)
			return
		}
		if err != nil {
			unavailable(writer)
			return
		}
		_ = json.NewEncoder(writer).Encode(struct {
			Endpoint string `json:"endpoint"`
		}{endpoint})
		return
	}
	var err error
	if request.URL.Path == "/observe" {
		ready, err := h.broker.Observe(request.Context(), message.Group)
		if errors.Is(err, ErrDenied) {
			writer.WriteHeader(403)
			return
		}
		if err != nil {
			unavailable(writer)
			return
		}
		_ = json.NewEncoder(writer).Encode(struct {
			Ready bool `json:"ready"`
		}{ready})
		return
	}
	if request.URL.Path == "/start" {
		err = h.broker.EnsureReady(request.Context(), message.Group)
	} else {
		err = h.broker.Stop(request.Context(), message.Group)
	}
	if errors.Is(err, ErrDenied) {
		writer.WriteHeader(403)
		return
	}
	if err != nil {
		unavailable(writer)
		return
	}
	writer.WriteHeader(204)
}

type ControlClient struct {
	endpoint string
	token    string
	http     *http.Client
}

func NewUnixControlClient(socket, token string) (*ControlClient, error) {
	if !tokenPattern.MatchString(token) {
		return nil, ErrDenied
	}
	client, err := unixHTTPClient(socket)
	if err != nil {
		return nil, err
	}
	return newControlHTTPClient("http://control", token, client), nil
}

func newControlHTTPClient(endpoint, token string, client *http.Client) *ControlClient {
	return &ControlClient{endpoint: endpoint, token: token, http: client}
}

func (c *ControlClient) action(ctx context.Context, path, group string) error {
	body, err := json.Marshal(controlRequest{Group: group})
	if err != nil {
		return ErrDenied
	}
	request, err := http.NewRequestWithContext(ctx, "POST", c.endpoint+path, bytes.NewReader(body))
	if err != nil {
		return ErrUnavailable
	}
	request.GetBody = nil
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Content-Type", "application/json")
	var headersSent atomic.Bool
	trace := &httptrace.ClientTrace{WroteHeaders: func() { headersSent.Store(true) }}
	request = request.WithContext(httptrace.WithClientTrace(request.Context(), trace))
	response, err := c.http.Do(request)
	if err != nil {
		if !headersSent.Load() {
			return ErrControlUnavailable
		}
		return ErrUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode == 204 {
		return nil
	}
	if response.StatusCode == 401 || response.StatusCode == 403 {
		return ErrDenied
	}
	return ErrUnavailable
}

func (c *ControlClient) EnsureReady(ctx context.Context, group string) error {
	return c.action(ctx, "/start", group)
}
func (c *ControlClient) Stop(ctx context.Context, group string) error {
	return c.action(ctx, "/stop", group)
}

func (c *ControlClient) Observe(ctx context.Context, group string) (bool, error) {
	body, err := json.Marshal(controlRequest{Group: group})
	if err != nil {
		return false, ErrDenied
	}
	request, err := http.NewRequestWithContext(ctx, "POST", c.endpoint+"/observe", bytes.NewReader(body))
	if err != nil {
		return false, ErrUnavailable
	}
	request.GetBody = nil
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.http.Do(request)
	if err != nil {
		return false, ErrUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode == 401 || response.StatusCode == 403 {
		return false, ErrDenied
	}
	if response.StatusCode != 200 {
		return false, ErrUnavailable
	}
	data, readErr := io.ReadAll(io.LimitReader(response.Body, 1025))
	if readErr != nil || len(data) > 1024 {
		return false, ErrUnavailable
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var result struct {
		Ready *bool `json:"ready"`
	}
	if decoder.Decode(&result) != nil || decoder.Decode(&struct{}{}) != io.EOF || result.Ready == nil {
		return false, ErrUnavailable
	}
	return *result.Ready, nil
}

func (c *ControlClient) Endpoint(ctx context.Context, group string) (string, error) {
	body, err := json.Marshal(controlRequest{Group: group})
	if err != nil {
		return "", ErrDenied
	}
	request, err := http.NewRequestWithContext(ctx, "POST", c.endpoint+"/endpoint", bytes.NewReader(body))
	if err != nil {
		return "", ErrUnavailable
	}
	request.GetBody = nil
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.http.Do(request)
	if err != nil {
		return "", ErrUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode == 401 || response.StatusCode == 403 {
		return "", ErrDenied
	}
	if response.StatusCode != 200 {
		return "", ErrUnavailable
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 1025))
	if err != nil || len(data) > 1024 {
		return "", ErrUnavailable
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var result struct {
		Endpoint string `json:"endpoint"`
	}
	if decoder.Decode(&result) != nil || decoder.Decode(&struct{}{}) != io.EOF || result.Endpoint == "" {
		return "", ErrUnavailable
	}
	return result.Endpoint, nil
}
