// Команда agent — универсальный агент: вид и параметры берутся из файла роли.
//
//	agent -role ../roles/triage.md
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/bus"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/config"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/logging"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/registry"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/role"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/schedule"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/stats"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/telemetry"
)

const (
	exitOK     = 0
	exitFailed = 1
	exitConfig = 2 // роль/настройки некорректны
)

func main() { os.Exit(run()) }

func run() int {
	rolePath := flag.String("role", "", "путь к файлу роли (roles/<role>.md)")
	flag.Parse()
	if *rolePath == "" {
		fmt.Fprintln(os.Stderr, "ошибка: не указан файл роли (-role)")
		return exitConfig
	}
	src, err := os.ReadFile(*rolePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка: не удалось прочитать файл роли: %v\n", err)
		return exitConfig
	}
	r, err := role.Parse(string(src), registry.Kinds())
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка в файле роли %s: %v\n", *rolePath, err)
		return exitConfig
	}
	cfg, err := config.FromEnv(os.Getenv, r.Step, os.Getpid())
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка настроек: %v\n", err)
		return exitConfig
	}
	loc, err := schedule.LoadLocation(cfg.ClinicTZ)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка настроек CLINIC_TZ: %v\n", err)
		return exitConfig
	}
	log, closeLog, err := logging.New(cfg.LogLevel, cfg.LogFile, cfg.AgentID)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ошибка: LOG_FILE: %v\n", err)
		return exitFailed
	}
	defer closeLog()
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	stopTelemetry, err := telemetry.Setup(ctx, "agent-"+r.Step, os.Getenv)
	if err != nil {
		log.Warn("телеметрия отключена: не удалось настроить экспорт", "err", err)
		stopTelemetry = func() {}
	}
	defer stopTelemetry()

	entry := registry.MustLookup(r.Kind)
	st, err := openStore(ctx, cfg, entry.RedisOptional, log)
	if err != nil {
		log.Error("Redis недоступен", "err", err)
		return exitFailed
	}
	defer st.Close()

	clock := core.Clock(func() time.Time { return time.Now().UTC() })
	started := clock.Now()
	counters := stats.New(st, r.Step, cfg.AgentID, log)
	counters.Load(ctx)

	b, err := bus.Connect(ctx, cfg.NATSURL, bus.Info{
		AgentID: cfg.AgentID, Step: r.Step, Kind: r.Kind, Title: r.Title, StartedAt: started,
	}, clock, log, counters)
	if err != nil {
		log.Error("не удалось подключиться к шине", "err", err)
		return exitFailed
	}

	inst, err := entry.New(core.Deps{
		AgentID: cfg.AgentID, Step: r.Step, Store: st, Clock: clock, Loc: loc, Sink: b, Log: log,
		ReminderDemoDelay: cfg.ReminderDemo, SurveyDemoDelay: cfg.SurveyDemo,
	}, r.Params)
	if err != nil {
		b.Close()
		fmt.Fprintf(os.Stderr, "ошибка в параметрах роли %s: %v\n", *rolePath, err)
		return exitConfig
	}

	for _, s := range inst.Subscriptions {
		if _, err := b.Subscribe(s); err != nil {
			log.Error("подписка не удалась", "subject", s.Subject, "err", err)
			b.Close()
			return exitFailed
		}
	}
	var wg sync.WaitGroup
	for _, bg := range inst.Background {
		wg.Add(1)
		go func() { defer wg.Done(); bg(ctx) }()
	}

	hbCtx, hbCancel := context.WithCancel(context.Background())
	var hbDone sync.WaitGroup
	hbDone.Add(1)
	go func() { defer hbDone.Done(); b.RunHeartbeat(hbCtx) }()

	log.Info("агент запущен", "kind", r.Kind, "step", r.Step, "title", r.Title)
	if inst.Tasks != nil {
		consumer, err := b.EnsureConsumer(ctx, r.Step)
		if err != nil {
			log.Error("не удалось подготовить очередь", "err", err)
			hbCancel()
			hbDone.Wait()
			b.Close()
			return exitFailed
		}
		b.RunTasks(ctx, consumer, inst.Tasks, r.Concurrency, bus.Chaos{FailRate: cfg.FailRate, Slow: cfg.Slow})
	} else {
		<-ctx.Done()
	}

	// Остановка (раздел 13): выборка уже прекращена, текущие задачи дорабатывают в RunTasks.
	log.Info("остановка агента")
	wg.Wait()
	hbCancel()
	hbDone.Wait()
	b.SendHeartbeat("stopping")
	b.Close()
	return exitOK
}

func openStore(ctx context.Context, cfg config.Config, optional bool, log *slog.Logger) (store.Store, error) {
	if optional && !cfg.RedisSet {
		log.Info("REDIS_URL не задан: счётчики хранятся в памяти")
		return store.NewMemory(), nil
	}
	rs, err := store.NewRedis(cfg.RedisURL)
	if err != nil {
		return nil, err
	}
	pctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if err := rs.Ping(pctx); err != nil {
		_ = rs.Close()
		if optional {
			log.Warn("Redis недоступен, используется память", "err", err)
			return store.NewMemory(), nil
		}
		return nil, err
	}
	return rs, nil
}
