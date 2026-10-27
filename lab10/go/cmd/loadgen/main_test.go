package main

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestRunCountsRequests(t *testing.T) {
	var served atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if served.Add(1)%10 == 0 {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte("ok"))
	}))
	defer srv.Close()

	res := run(srv.Client(), http.MethodGet, srv.URL, nil, 4, 200*time.Millisecond)
	if res.Requests == 0 || int64(res.Requests) != served.Load() {
		t.Fatalf("requests %d, served %d", res.Requests, served.Load())
	}
	if res.Non2xx == 0 || res.Errors != 0 {
		t.Fatalf("non2xx %d errors %d", res.Non2xx, res.Errors)
	}
	if res.Latency.P50 > res.Latency.P99 || res.Latency.P99 > res.Latency.Max {
		t.Fatalf("перцентили не упорядочены: %+v", res.Latency)
	}
}

func TestSummarize(t *testing.T) {
	var lat []time.Duration
	for i := 1; i <= 100; i++ {
		lat = append(lat, time.Duration(i)*time.Millisecond)
	}
	l := summarize(lat)
	// Nearest-rank: p50 из 1..100 — 50-е значение, а не 51-е.
	if l.P50 != 50 || l.P90 != 90 || l.P99 != 99 || l.Max != 100 || l.Mean != 50.5 {
		t.Fatalf("%+v", l)
	}
	if summarize(nil) != (Latency{}) {
		t.Fatal("пустой список")
	}
	one := summarize([]time.Duration{7 * time.Millisecond})
	if one.P50 != 7 || one.P99 != 7 {
		t.Fatalf("один замер: %+v", one)
	}
}

func TestRunBacksOffWhenServerDown(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close() // соединения будут отклоняться

	res := run(http.DefaultClient, http.MethodGet, url, nil, 2, 100*time.Millisecond)
	// Без паузы за 100 мс набралось бы много тысяч ошибок; с паузой 10 мс — десятки.
	if res.Errors == 0 || res.Errors > 50 {
		t.Fatalf("ошибок %d: пауза после ошибки не работает", res.Errors)
	}
}
