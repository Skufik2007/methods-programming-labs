package server

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"
)

// startSlow запускает Run с обработчиком, который отвечает через delay
// и сообщает в started, что запрос начал обрабатываться.
func startSlow(t *testing.T, delay time.Duration, opt Options) (url string, started chan struct{}, cancel context.CancelFunc, done chan error) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	started = make(chan struct{}, 1)
	mux := http.NewServeMux()
	mux.HandleFunc("/slow", func(w http.ResponseWriter, r *http.Request) {
		started <- struct{}{}
		time.Sleep(delay)
		_, _ = io.WriteString(w, "done")
	})
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: time.Second}

	ctx, cancel := context.WithCancel(context.Background())
	done = make(chan error, 1)
	opt.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	go func() { done <- Run(ctx, srv, ln, opt) }()
	return "http://" + ln.Addr().String(), started, cancel, done
}

func TestGracefulShutdownWaitsForInFlight(t *testing.T) {
	ready := &atomic.Bool{}
	url, started, cancel, done := startSlow(t, 300*time.Millisecond, Options{
		ShutdownTimeout: 5 * time.Second, Ready: ready,
	})

	type result struct {
		body string
		err  error
	}
	res := make(chan result, 1)
	go func() {
		resp, err := http.Get(url + "/slow")
		if err != nil {
			res <- result{err: err}
			return
		}
		defer resp.Body.Close()
		b, err := io.ReadAll(resp.Body)
		res <- result{string(b), err}
	}()

	<-started
	if !ready.Load() {
		t.Fatal("до остановки Ready должен быть true")
	}
	cancel() // имитация SIGTERM во время обработки запроса

	r := <-res
	if r.err != nil || r.body != "done" {
		t.Fatalf("активный запрос прерван: %q %v", r.body, r.err)
	}
	if err := <-done; err != nil {
		t.Fatalf("Run вернул ошибку: %v", err)
	}
	if ready.Load() {
		t.Fatal("после остановки Ready должен быть false")
	}
	if _, err := http.Get(url + "/slow"); err == nil {
		t.Fatal("после остановки сервер не должен принимать соединения")
	}
}

func TestGracefulShutdownTimeout(t *testing.T) {
	url, started, cancel, done := startSlow(t, 3*time.Second, Options{ShutdownTimeout: 200 * time.Millisecond})

	go func() {
		resp, err := http.Get(url + "/slow")
		if err == nil {
			resp.Body.Close()
		}
	}()
	<-started
	begin := time.Now()
	cancel()

	if err := <-done; err == nil {
		t.Fatal("при превышении тайм-аута Run должен вернуть ошибку")
	}
	if took := time.Since(begin); took > 2*time.Second {
		t.Fatalf("остановка заняла %v — тайм-аут не сработал", took)
	}
}

func TestDrainDelay(t *testing.T) {
	ready := &atomic.Bool{}
	_, _, cancel, done := startSlow(t, 0, Options{
		ShutdownTimeout: time.Second, DrainDelay: 200 * time.Millisecond, Ready: ready,
	})
	time.Sleep(50 * time.Millisecond)
	begin := time.Now()
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if took := time.Since(begin); took < 200*time.Millisecond {
		t.Fatalf("DrainDelay не выдержан: %v", took)
	}
}
