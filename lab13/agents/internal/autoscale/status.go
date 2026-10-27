package autoscale

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"
)

// StepStatus — состояние шага для GET /status.
type StepStatus struct {
	Step          string    `json:"step"`
	ConsumerFound bool      `json:"consumer_found"`
	Backlog       int       `json:"backlog"`
	PerInstance   int       `json:"per_instance"`
	Min           int       `json:"min"`
	Max           int       `json:"max"`
	Static        int       `json:"static"`  // экземпляры вне автомасштабировщика (min)
	Auto          []string  `json:"auto"`    // добавочные экземпляры (AGENT_ID)
	Total         int       `json:"total"`   // static + len(auto)
	Desired       int       `json:"desired"` // желаемое общее число
	LastGrowth    string    `json:"last_growth,omitempty"`
	UpdatedAt     time.Time `json:"updated_at"`
}

// Status — ответ GET /status.
type Status struct {
	Steps []StepStatus `json:"steps"`
}

// Snapshot возвращает копию текущего состояния.
func (s *Scaler) Snapshot() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := Status{Steps: make([]StepStatus, 0, len(s.steps))}
	for _, sc := range s.steps {
		v := s.state[sc.Step].status
		v.Auto = append([]string{}, v.Auto...)
		out.Steps = append(out.Steps, v)
	}
	return out
}

// ServeStatus поднимает HTTP-сервер со /status и блокируется до отмены ctx.
func (s *Scaler) ServeStatus(ctx context.Context, port int) error {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Access-Control-Allow-Origin", "*") // дашборд открывается с другого порта
		_ = json.NewEncoder(w).Encode(s.Snapshot())
	})
	srv := &http.Server{Addr: fmt.Sprintf(":%d", port), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	select {
	case err := <-errc:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		sctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		slog.Debug("остановка HTTP /status")
		return srv.Shutdown(sctx)
	}
}
