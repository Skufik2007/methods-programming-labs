package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"runtime"
	"strconv"
	"time"
)

const (
	maxPrimesLimit = 2_000_000_000
	maxMatrixSize  = 1500
	maxWorkers     = 256
)

// NewHandler собирает маршруты сервиса. maxJobs ограничивает число
// одновременных тяжёлых вычислений, чтобы сервис не съел всю память и CPU.
func NewHandler(maxJobs int) http.Handler {
	if maxJobs <= 0 {
		maxJobs = 1
	}
	jobs := make(chan struct{}, maxJobs)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "cpus": runtime.NumCPU()})
	})
	mux.HandleFunc("GET /primes", limited(jobs, handlePrimes))
	mux.HandleFunc("POST /matmul", limited(jobs, handleMatmul))
	return mux
}

// limited пропускает запрос, только когда есть свободный слот; ожидание
// прерывается, если клиент отключился.
func limited(jobs chan struct{}, h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		select {
		case jobs <- struct{}{}:
			defer func() { <-jobs }()
			h(w, r)
		case <-r.Context().Done():
			writeError(w, http.StatusServiceUnavailable, "запрос отменён до начала вычисления")
		}
	}
}

type PrimesResponse struct {
	Limit     int     `json:"limit"`
	Count     int     `json:"count"`
	Workers   int     `json:"workers"`
	ElapsedMs float64 `json:"elapsed_ms"`
}

func handlePrimes(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit, err := intParam(q.Get("limit"), -1, 0, maxPrimesLimit)
	if err != nil || limit < 0 {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("параметр limit обязателен, 0..%d", maxPrimesLimit))
		return
	}
	workers, err := intParam(q.Get("workers"), runtime.NumCPU(), 1, maxWorkers)
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("workers должен быть в диапазоне 1..%d", maxWorkers))
		return
	}

	start := time.Now()
	count := CountPrimes(limit, workers)
	writeJSON(w, http.StatusOK, PrimesResponse{
		Limit: limit, Count: count, Workers: workers, ElapsedMs: ms(time.Since(start)),
	})
}

type MatmulRequest struct {
	Size    int   `json:"size"`
	Seed    int64 `json:"seed"`
	Workers int   `json:"workers"`
}

type MatmulResponse struct {
	Size      int     `json:"size"`
	Trace     float64 `json:"trace"`
	Workers   int     `json:"workers"`
	ElapsedMs float64 `json:"elapsed_ms"`
}

func handleMatmul(w http.ResponseWriter, r *http.Request) {
	var req MatmulRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "некорректный JSON: "+err.Error())
		return
	}
	if req.Size < 1 || req.Size > maxMatrixSize {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("size должен быть в диапазоне 1..%d", maxMatrixSize))
		return
	}
	if req.Workers == 0 {
		req.Workers = runtime.NumCPU()
	}
	if req.Workers < 1 || req.Workers > maxWorkers {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("workers должен быть в диапазоне 1..%d", maxWorkers))
		return
	}

	start := time.Now()
	a := RandomMatrix(req.Size, req.Seed)
	b := RandomMatrix(req.Size, req.Seed+1)
	c := MatMul(a, b, req.Workers)
	writeJSON(w, http.StatusOK, MatmulResponse{
		Size: req.Size, Trace: Trace(c), Workers: req.Workers, ElapsedMs: ms(time.Since(start)),
	})
}

// intParam разбирает целый параметр; пустая строка даёт значение по умолчанию.
func intParam(s string, def, lo, hi int) (int, error) {
	if s == "" {
		return def, nil
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return 0, err
	}
	if v < lo || v > hi {
		return 0, fmt.Errorf("значение %d вне диапазона %d..%d", v, lo, hi)
	}
	return v, nil
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
