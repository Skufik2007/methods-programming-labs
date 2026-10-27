// Команда sensorsim — эмулятор носимых датчиков (docs/ARCHITECTURE.md, раздел 3).
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/config"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sim"
)

func main() { os.Exit(run()) }

func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil)).With("component", "sensorsim")
	cfg, err := config.LoadSim(os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "ошибка конфигурации:\n"+err.Error())
		return 2
	}
	clk := clock.Real{}
	if cfg.Start.IsZero() {
		cfg.Start = clk.Now()
	}
	gen := sim.New(sim.Params{
		Seed: cfg.Seed, Devices: cfg.Devices, PeriodMs: cfg.PeriodMs, Start: cfg.Start,
		Defects: sim.Defects{Range: cfg.DefectRange, Null: cfg.DefectNull, Dup: cfg.DefectDup,
			Unit: cfg.DefectUnit, Future: cfg.DefectFuture},
	})
	srv := sim.NewServer(gen, clk, time.Duration(cfg.LatencyMs)*time.Millisecond, cfg.LatencyJitterPct)
	hs := &http.Server{Addr: cfg.Addr, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	errc := make(chan error, 1)
	go func() { errc <- hs.ListenAndServe() }()
	log.Info("эмулятор запущен", "addr", cfg.Addr, "devices", cfg.Devices, "period_ms", cfg.PeriodMs,
		"seed", cfg.Seed, "latency_ms", cfg.LatencyMs, "start", cfg.Start)
	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Error("сервер остановился", "err", err)
			return 1
		}
	case <-ctx.Done():
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = hs.Shutdown(sctx)
		log.Info("эмулятор остановлен")
	}
	return 0
}
