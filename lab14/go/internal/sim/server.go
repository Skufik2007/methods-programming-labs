package sim

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

type Server struct {
	gen        *Generator
	clk        clock.Clock
	latency    time.Duration
	jitterPct  int64
	reqDevices atomic.Int64
	reqReads   atomic.Int64
	served     atomic.Int64
	dupsServed atomic.Int64
	inflight   atomic.Int64
}

func NewServer(g *Generator, clk clock.Clock, latency time.Duration, jitterPct int64) *Server {
	return &Server{gen: g, clk: clk, latency: latency, jitterPct: jitterPct}
}

type deviceInfo struct {
	DeviceID  string   `json:"device_id"`
	PatientID string   `json:"patient_id"`
	Metrics   []string `json:"metrics"`
	PeriodMs  int64    `json:"period_ms"`
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /metrics", s.metrics)
	mux.HandleFunc("GET /v1/devices", s.devices)
	mux.HandleFunc("GET /v1/devices/{id}/readings", s.readings)
	return mux
}

// delay имитирует задержку сети/датчика: LATENCY_MS ± джиттер. Возвращает false,
// если клиент ушёл раньше.
func (s *Server) delay(r *http.Request) bool {
	if s.latency <= 0 {
		return true
	}
	d := s.latency
	if s.jitterPct > 0 {
		spread := float64(s.jitterPct) / 100
		d = time.Duration(float64(d) * (1 + spread*(2*rand.Float64()-1)))
	}
	select {
	case <-s.clk.After(d):
		return true
	case <-r.Context().Done():
		return false
	}
}

func (s *Server) devices(w http.ResponseWriter, r *http.Request) {
	s.reqDevices.Add(1)
	if !s.delay(r) {
		return
	}
	list := make([]deviceInfo, 0, s.gen.Devices())
	for n := 1; n <= s.gen.Devices(); n++ {
		list = append(list, deviceInfo{s.gen.DeviceID(n), s.gen.PatientID(n), model.Metrics, s.gen.PeriodMs()})
	}
	writeJSON(w, http.StatusOK, map[string]any{"devices": list})
}

func (s *Server) readings(w http.ResponseWriter, r *http.Request) {
	s.reqReads.Add(1)
	s.inflight.Add(1)
	defer s.inflight.Add(-1)
	n, ok := s.gen.DeviceIndex(r.PathValue("id"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown device"})
		return
	}
	q := r.URL.Query()
	after, limit := int64(0), 1000
	if v := q.Get("after_seq"); v != "" {
		var err error
		if after, err = strconv.ParseInt(v, 10, 64); err != nil || after < 0 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad after_seq"})
			return
		}
	}
	if v := q.Get("limit"); v != "" {
		l, err := strconv.Atoi(v)
		if err != nil || l < 1 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad limit"})
			return
		}
		limit = min(l, 1000)
	}
	if !s.delay(r) {
		return
	}
	// время фиксируется после задержки: «уже случившиеся» к моменту ответа
	rs, last, dups := s.gen.Readings(n, after, limit, s.clk.Now())
	s.served.Add(int64(len(rs)))
	s.dupsServed.Add(int64(dups))
	writeJSON(w, http.StatusOK, struct {
		DeviceID string          `json:"device_id"`
		Readings []model.Reading `json:"readings"`
		LastSeq  int64           `json:"last_seq"`
	}{s.gen.DeviceID(n), rs, last})
}

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# TYPE sensorsim_requests_total counter\n")
	fmt.Fprintf(w, "sensorsim_requests_total{endpoint=\"devices\"} %d\n", s.reqDevices.Load())
	fmt.Fprintf(w, "sensorsim_requests_total{endpoint=\"readings\"} %d\n", s.reqReads.Load())
	fmt.Fprintf(w, "# TYPE sensorsim_readings_served_total counter\nsensorsim_readings_served_total %d\n", s.served.Load())
	fmt.Fprintf(w, "# TYPE sensorsim_duplicates_served_total counter\nsensorsim_duplicates_served_total %d\n", s.dupsServed.Load())
	fmt.Fprintf(w, "# TYPE sensorsim_inflight_requests gauge\nsensorsim_inflight_requests %d\n", s.inflight.Load())
	fmt.Fprintf(w, "# TYPE sensorsim_devices gauge\nsensorsim_devices %d\n", s.gen.Devices())
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
