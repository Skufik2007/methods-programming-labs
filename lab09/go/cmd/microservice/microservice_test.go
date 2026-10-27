package main

import (
	"encoding/json"
	"math"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCountPrimes(t *testing.T) {
	// Известные значения функции π(n).
	cases := map[int]int{0: 0, 1: 0, 2: 1, 10: 4, 100: 25, 1000: 168, 1_000_000: 78498, 10_000_000: 664579}
	for limit, want := range cases {
		for _, workers := range []int{1, 4} {
			if got := CountPrimes(limit, workers); got != want {
				t.Errorf("CountPrimes(%d, %d) = %d, want %d", limit, workers, got, want)
			}
		}
	}
}

func TestMatMul(t *testing.T) {
	a := [][]float64{{1, 2}, {3, 4}}
	b := [][]float64{{5, 6}, {7, 8}}
	c := MatMul(a, b, 2)
	want := [][]float64{{19, 22}, {43, 50}}
	for i := range want {
		for j := range want[i] {
			if c[i][j] != want[i][j] {
				t.Fatalf("c = %v, want %v", c, want)
			}
		}
	}
}

func TestMatMulWorkersIndependent(t *testing.T) {
	a, b := RandomMatrix(50, 1), RandomMatrix(50, 2)
	t1 := Trace(MatMul(a, b, 1))
	t8 := Trace(MatMul(a, b, 8))
	if math.Abs(t1-t8) > 1e-9 {
		t.Fatalf("trace depends on workers: %v vs %v", t1, t8)
	}
}

func TestHandlers(t *testing.T) {
	h := NewHandler(2)

	cases := []struct {
		method, path, body string
		status             int
	}{
		{"GET", "/health", "", 200},
		{"GET", "/primes?limit=100", "", 200},
		{"GET", "/primes", "", 400},
		{"GET", "/primes?limit=abc", "", 400},
		{"GET", "/primes?limit=-5", "", 400},
		{"GET", "/primes?limit=10&workers=0", "", 400},
		{"POST", "/matmul", `{"size":10,"seed":1}`, 200},
		{"POST", "/matmul", `{"size":0}`, 400},
		{"POST", "/matmul", `{"size":10,"extra":1}`, 400},
		{"POST", "/matmul", `not json`, 400},
		{"GET", "/matmul", "", 405},
	}
	for _, c := range cases {
		req := httptest.NewRequest(c.method, c.path, strings.NewReader(c.body))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != c.status {
			t.Errorf("%s %s: status %d, want %d (%s)", c.method, c.path, rec.Code, c.status, rec.Body)
		}
	}

	req := httptest.NewRequest("GET", "/primes?limit=1000", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var resp PrimesResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Count != 168 {
		t.Fatalf("count = %d, want 168", resp.Count)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("content-type = %q", ct)
	}
}
