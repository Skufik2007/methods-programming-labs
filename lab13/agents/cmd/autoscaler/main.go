// Команда autoscaler — автомасштабировщик агентов (раздел 12 контракта).
//
//	autoscaler -config deploy/autoscaler.local.yaml
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/autoscale"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/logging"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/telemetry"
)

func main() { os.Exit(run()) }

func run() int {
	cfgPath := flag.String("config", "deploy/autoscaler.local.yaml", "путь к YAML-конфигурации")
	flag.Parse()

	cfg, err := autoscale.LoadConfig(*cfgPath, os.Getenv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка конфигурации: %v\n", err)
		return 2
	}
	log, closeLog, err := logging.New(envOr("LOG_LEVEL", "info"), os.Getenv("LOG_FILE"), "autoscaler")
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка: LOG_FILE: %v\n", err)
		return 1
	}
	defer closeLog()
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	stopTelemetry, err := telemetry.Setup(ctx, "autoscaler", os.Getenv)
	if err != nil {
		log.Warn("телеметрия отключена: не удалось настроить экспорт", "err", err)
		stopTelemetry = func() {}
	}
	defer stopTelemetry()

	clock := core.Clock(func() time.Time { return time.Now().UTC() })
	nc, err := autoscale.ConnectNATS(ctx, cfg.NATSURL, clock, log)
	if err != nil {
		log.Error("не удалось подключиться к шине", "err", err)
		return 1
	}
	defer nc.Close()

	var backend autoscale.Backend
	switch cfg.Backend {
	case "docker":
		backend = autoscale.NewDockerBackend(cfg.Docker, log)
	default:
		backend = autoscale.NewExecBackend(cfg.Exec, log)
	}

	scaler := autoscale.New(cfg.Steps, cfg.Interval, nc, backend, nc, clock, log)
	go func() {
		if err := scaler.ServeStatus(ctx, cfg.StatusPort); err != nil {
			log.Error("HTTP /status не работает", "err", err)
		}
	}()

	log.Info("автомасштабировщик запущен", "backend", cfg.Backend, "interval", cfg.Interval.String(),
		"steps", len(cfg.Steps), "status_port", cfg.StatusPort)
	scaler.Run(ctx)

	// Сигнал получен: останавливаем свои добавочные экземпляры (контекст — новый, не отменённый).
	log.Info("остановка автомасштабировщика")
	scaler.Shutdown(context.Background())
	return 0
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
