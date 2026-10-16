package idle

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDockerClientUsesOnlyExactBoundedContainerOperations(t *testing.T) {
	id := strings.Repeat("a", 64)
	var operations []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		operations = append(operations, r.Method+" "+r.URL.RequestURI())
		if r.Method == "GET" {
			_, _ = fmt.Fprintf(w, `{"Id":%q,"Config":{"Image":"fixture:local","Env":["PASSWORD=PRIVATE-MATERIAL"],"Labels":{"com.xynes.managed":"true","private":"PRIVATE-MATERIAL"}},"State":{"Running":true,"Health":{"Status":"healthy"}}}`, id)
		} else {
			w.WriteHeader(204)
		}
	}))
	defer server.Close()
	client := newContainerHTTPClient(server.URL, server.Client())
	info, err := client.Inspect(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if info.ID != id || !info.Running || !info.Healthy || strings.Contains(fmt.Sprintf("%+v", info), "PRIVATE") {
		t.Fatal("unsafe Docker inspect projection")
	}
	if err := client.Start(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	if err := client.Stop(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	expected := []string{"GET /v1.49/containers/" + id + "/json", "POST /v1.49/containers/" + id + "/start", "POST /v1.49/containers/" + id + "/stop?t=60"}
	if fmt.Sprint(operations) != fmt.Sprint(expected) {
		t.Fatal("unexpected Docker operation")
	}
}

func TestDockerClientRefusesPathInjectionBeforeNetwork(t *testing.T) {
	client := newContainerHTTPClient("http://127.0.0.1:1", http.DefaultClient)
	for _, id := range []string{"prod-db", "../images", "a/exec", strings.Repeat("a", 63)} {
		if _, err := client.Inspect(context.Background(), id); err != ErrDenied {
			t.Fatal("invalid ID inspected")
		}
		if err := client.Start(context.Background(), id); err != ErrDenied {
			t.Fatal("invalid ID started")
		}
		if err := client.Stop(context.Background(), id); err != ErrDenied {
			t.Fatal("invalid ID stopped")
		}
	}
}

func TestDockerClientRedactsHTTPAndMalformedPayloadFailures(t *testing.T) {
	for _, status := range []int{500, 200} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(status)
			_, _ = w.Write([]byte("PRIVATE-MATERIAL malformed JSON"))
		}))
		client := newContainerHTTPClient(server.URL, server.Client())
		if _, err := client.Inspect(context.Background(), strings.Repeat("a", 64)); err != ErrUnavailable {
			t.Fatal("unsafe daemon failure")
		}
		server.Close()
	}
}
