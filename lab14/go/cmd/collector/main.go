// Команда collector — сборщик показаний (docs/ARCHITECTURE.md, раздел 4).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/collector"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/config"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/coord"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks/files"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks/flightsink"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks/natssink"
)

func main() { os.Exit(run()) }

func run() int {
	duration := flag.Duration("duration", 0, "завершиться через указанное время тем же путём, что и по сигналу (0 — работать до сигнала)")
	flag.Parse()

	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	cfg, err := config.LoadCollector(os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "ошибка конфигурации:\n"+err.Error())
		return 2
	}
	log = log.With("component", "collector", "collector_id", cfg.CollectorID)
	clk := clock.Real{}

	reg := sinks.NewRegistry()
	reg.Register("files", files.Factory)
	reg.Register("nats", natssink.Factory)
	reg.Register("flight", flightsink.Factory)
	named, err := reg.Build(cfg.Sinks, sinks.Env{Config: cfg, Clock: clk, Log: log})
	if err != nil {
		fmt.Fprintln(os.Stderr, "ошибка конфигурации: "+err.Error())
		return 2
	}
	src := collector.NewHTTPSource(cfg.SimURL, cfg.HTTPTimeout, cfg.Workers)
	var (
		cps collector.CheckpointStore
		asg collector.Assigner
	)
	switch cfg.Coord {
	case "etcd":
		ea, err := dialEtcd(cfg, named, src, log)
		if err != nil {
			for _, s := range named {
				_ = s.Sink.Close()
			}
			fmt.Fprintln(os.Stderr, "etcd: "+err.Error())
			return 1
		}
		cps, asg = ea.Checkpoints(), ea
	default: // static: прежние реализации
		fs, err := collector.NewFileStore(filepath.Join(cfg.OutDir, "checkpoints.json"))
		if err != nil {
			fmt.Fprintln(os.Stderr, "чекпоинты: "+err.Error())
			return 1
		}
		cps, asg = fs, collector.NewStaticAssigner(cfg.DeviceList, src.Devices)
	}
	c, err := collector.New(collector.Deps{
		Config: cfg, Log: log, Clock: clk, Source: src, Checkpoints: cps, Sinks: named,
		Assigner: asg,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *duration > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, *duration)
		defer cancel()
	}
	start := time.Now()
	if err := c.Run(ctx); err != nil {
		log.Error("завершение с ошибкой", "err", err, "uptime", time.Since(start).String())
		return 1
	}
	return 0
}

// dialEtcd регистрирует сборщика в etcd. flight_addr берётся у стока flight, если он включён.
func dialEtcd(cfg config.Collector, named []sinks.Named, src *collector.HTTPSource, log *slog.Logger) (*coord.Assigner, error) {
	self := coord.Member{CollectorID: cfg.CollectorID, StatusAddr: cfg.StatusAddr,
		StartedAt: time.Now().UTC().Format(time.RFC3339)}
	for _, s := range named {
		if a, ok := s.Sink.(interface{ Addr() string }); ok {
			self.FlightAddr = a.Addr()
		}
	}
	devices := src.Devices
	if len(cfg.DeviceList) > 0 {
		devices = func(context.Context) ([]string, error) { return cfg.DeviceList, nil }
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	return coord.Dial(ctx, coord.Options{
		Endpoints: cfg.EtcdEndpoints, TTL: cfg.EtcdTTL, Self: self, Devices: devices, Log: log,
		// старый владелец успевает прекратить опрос, сбросить пакет и записать чекпоинт
		Handover: cfg.FlushInterval + cfg.PollInterval + time.Second,
	})
}
