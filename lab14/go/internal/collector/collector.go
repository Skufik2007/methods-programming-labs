// Пакет collector — ядро сборщика (docs/ARCHITECTURE.md, раздел 4): опрос пулом
// горутин → канал → окна и пакетная запись в стоки → чекпоинты.
package collector

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/config"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/window"
)

// windowTick — как часто проверяются окна, готовые к закрытию.
const windowTick = 500 * time.Millisecond

type Deps struct {
	Config      config.Collector
	Log         *slog.Logger
	Clock       clock.Clock
	Source      Source
	Assigner    Assigner
	Checkpoints CheckpointStore
	Sinks       []sinks.Named
}

type Collector struct {
	cfg   config.Collector
	log   *slog.Logger
	clk   clock.Clock
	src   Source
	asg   Assigner
	cps   CheckpointStore
	sinks []sinks.Named
	agg   *window.Aggregator

	in      chan model.Reading
	m       metrics
	started time.Time

	devices atomic.Pointer[[]string]

	curMu   sync.Mutex
	cursors map[string]int64 // что уже отправлено в канал (≠ чекпоинт: тот — после Flush)
	flight  map[string]bool  // устройства, которые сейчас опрашиваются

	abort    context.Context // отменяется по истечении SHUTDOWN_TIMEOUT после начала остановки
	abortFn  context.CancelFunc
	listener net.Listener
}

func New(d Deps) (*Collector, error) {
	if d.Clock == nil || d.Source == nil || d.Assigner == nil || d.Checkpoints == nil || d.Log == nil {
		return nil, errors.New("collector: не заданы зависимости")
	}
	c := &Collector{
		cfg: d.Config, log: d.Log, clk: d.Clock, src: d.Source, asg: d.Assigner,
		cps: d.Checkpoints, sinks: d.Sinks,
		agg:     window.New(d.Config.CollectorID, d.Config.Window, d.Config.WindowLateness),
		in:      make(chan model.Reading, d.Config.Buffer),
		cursors: map[string]int64{}, flight: map[string]bool{},
	}
	c.abort, c.abortFn = context.WithCancel(context.Background())
	return c, nil
}

// Run работает, пока ctx не отменён, затем выполняет graceful shutdown в порядке
// раздела 4. statusAddr == "" — без HTTP.
func (c *Collector) Run(ctx context.Context) error {
	c.started = c.clk.Now()
	if c.cfg.StatusAddr != "" {
		ln, err := net.Listen("tcp", c.cfg.StatusAddr)
		if err != nil {
			return err
		}
		c.listener = ln
	}
	var hs *http.Server
	if c.listener != nil {
		hs = &http.Server{Handler: c.Handler(), ReadHeaderTimeout: 5 * time.Second}
		go func() { _ = hs.Serve(c.listener) }()
	}
	c.log.Info("сборщик запущен", "status_addr", c.cfg.StatusAddr,
		"workers", c.cfg.Workers, "emit", c.cfg.Emit, "window", c.cfg.Window.String())

	pipeDone := make(chan error, 1)
	go func() { pipeDone <- c.runPipeline() }()

	var pollWG sync.WaitGroup
	c.startPolling(ctx, &pollWG)

	<-ctx.Done()
	c.log.Info("остановка: шаг 1 — опрос прекращён")
	deadline := time.AfterFunc(c.cfg.ShutdownTimeout, c.abortFn)
	defer deadline.Stop()
	pollWG.Wait()
	c.log.Info("остановка: шаг 2 — ожидание обработки канала", "queued", len(c.in))
	close(c.in)
	errs := []error{<-pipeDone} // шаги 3–5: закрытие окон, сброс в стоки, чекпоинты

	sctx, cancel := context.WithTimeout(context.Background(), c.cfg.ShutdownTimeout)
	defer cancel()
	c.log.Info("остановка: шаг 6 — отзыв назначения (lease)")
	errs = append(errs, c.asg.Close(sctx))
	c.log.Info("остановка: шаг 7 — закрытие стоков и соединений")
	for _, s := range c.sinks {
		if err := s.Sink.Close(); err != nil {
			c.m.errors.Add(1)
			errs = append(errs, err)
		}
	}
	if hs != nil {
		errs = append(errs, hs.Shutdown(sctx))
	}
	c.abortFn()
	err := errors.Join(errs...)
	c.log.Info("сборщик остановлен", "err", err)
	return err
}
