package autoscale

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
)

const (
	tracerName   = "agents/autoscale"
	eventSource  = "autoscaler"
	opTimeout    = 60 * time.Second // потолок на запуск/остановку одного экземпляра
	shutdownWait = 90 * time.Second
)

// Observer читает длину очереди шага.
type Observer interface {
	// Backlog возвращает num_pending + num_ack_pending; found=false, если консьюмера ещё нет.
	Backlog(ctx context.Context, step string) (backlog int, found bool, err error)
}

// Emitter публикует события в журнал (events.autoscaler).
type Emitter interface {
	Emit(ctx context.Context, e core.Event)
}

// Scaler — цикл «наблюдать → решить → применить».
type Scaler struct {
	steps    []StepConfig
	obs      Observer
	backend  Backend
	emit     Emitter
	clock    core.Clock
	log      *slog.Logger
	interval time.Duration

	mu    sync.Mutex
	state map[string]*stepState
}

type stepState struct {
	prevBacklog int
	seen        bool
	lastGrowth  time.Time
	status      StepStatus
}

func New(steps []StepConfig, interval time.Duration, obs Observer, backend Backend, emit Emitter,
	clock core.Clock, log *slog.Logger) *Scaler {
	s := &Scaler{steps: steps, obs: obs, backend: backend, emit: emit, clock: clock, log: log,
		interval: interval, state: map[string]*stepState{}}
	for _, st := range steps {
		s.state[st.Step] = &stepState{status: StepStatus{Step: st.Step, PerInstance: st.PerInstance,
			Min: st.Min, Max: st.Max, Auto: []string{}}}
	}
	return s
}

// Run выполняет Reconcile сразу и затем каждые interval, пока ctx жив.
func (s *Scaler) Run(ctx context.Context) {
	t := time.NewTicker(s.interval)
	defer t.Stop()
	for {
		s.Reconcile(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Reconcile — один проход по всем шагам (один спан autoscaler.reconcile).
func (s *Scaler) Reconcile(ctx context.Context) {
	ctx, span := otel.Tracer(tracerName).Start(ctx, "autoscaler.reconcile")
	defer span.End()
	for _, sc := range s.steps {
		if ctx.Err() != nil {
			return
		}
		if err := s.reconcileStep(ctx, span, sc); err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
			s.log.Warn("ошибка согласования", "step", sc.Step, "err", err)
		}
	}
}

func (s *Scaler) reconcileStep(ctx context.Context, span trace.Span, sc StepConfig) error {
	now := s.clock.Now()
	st := s.state[sc.Step]

	backlog, found, err := s.obs.Backlog(ctx, sc.Step)
	if err != nil {
		return fmt.Errorf("чтение консьюмера: %w", err)
	}
	if !found {
		// Консьюмер создаёт первый запущенный агент шага; до этого масштабировать нечего.
		s.setStatus(st, func(v *StepStatus) { v.ConsumerFound = false; v.UpdatedAt = now })
		return nil
	}
	extras, err := s.backend.List(ctx, sc.Step)
	if err != nil {
		return fmt.Errorf("список экземпляров: %w", err)
	}

	s.mu.Lock()
	if !st.seen {
		st.seen, st.lastGrowth = true, now // после старта ждём cooldown, прежде чем уменьшать
	} else if backlog > st.prevBacklog {
		st.lastGrowth = now
	}
	st.prevBacklog = backlog
	lastGrowth := st.lastGrowth
	s.mu.Unlock()

	current := sc.Min + len(extras)
	desired := Desired(backlog, current, lastGrowth, now, sc.Params())
	wantExtra := desired - sc.Min
	if wantExtra < 0 {
		wantExtra = 0
	}
	span.SetAttributes(attribute.Int("backlog."+sc.Step, backlog), attribute.Int("desired."+sc.Step, desired))
	s.log.Debug("согласование", "step", sc.Step, "backlog", backlog, "current", current, "desired", desired)

	switch {
	case wantExtra > len(extras):
		started := 0
		for i := len(extras); i < wantExtra; i++ {
			inst, err := s.startOne(ctx, sc)
			if err != nil {
				s.log.Error("не удалось запустить экземпляр", "step", sc.Step, "err", err)
				span.RecordError(err)
				break
			}
			extras = append(extras, inst)
			started++
		}
		if started > 0 {
			s.mu.Lock()
			st.lastGrowth = now
			s.mu.Unlock()
			s.announce(ctx, "scaled_up", sc.Step, backlog, current, sc.Min+len(extras))
		}
	case wantExtra < len(extras):
		stopped := 0
		for len(extras) > wantExtra {
			victim := extras[len(extras)-1] // самый новый
			if err := s.stopOne(ctx, victim); err != nil {
				s.log.Error("не удалось остановить экземпляр", "step", sc.Step, "agent_id", victim.ID, "err", err)
				span.RecordError(err)
				break
			}
			extras = extras[:len(extras)-1]
			stopped++
		}
		if stopped > 0 {
			s.announce(ctx, "scaled_down", sc.Step, backlog, current, sc.Min+len(extras))
		}
	}

	ids := make([]string, 0, len(extras))
	for _, e := range extras {
		ids = append(ids, e.ID)
	}
	s.setStatus(st, func(v *StepStatus) {
		v.ConsumerFound, v.Backlog, v.Auto = true, backlog, ids
		v.Static, v.Total, v.Desired = sc.Min, sc.Min+len(ids), desired
		v.LastGrowth, v.UpdatedAt = lastGrowth.Format(time.RFC3339), now
	})
	return nil
}

func (s *Scaler) startOne(ctx context.Context, sc StepConfig) (Instance, error) {
	ctx, cancel := context.WithTimeout(ctx, opTimeout)
	defer cancel()
	inst, err := s.backend.Start(ctx, sc)
	if err == nil {
		s.log.Info("экземпляр запущен", "step", sc.Step, "agent_id", inst.ID)
	}
	return inst, err
}

func (s *Scaler) stopOne(ctx context.Context, inst Instance) error {
	ctx, cancel := context.WithTimeout(ctx, opTimeout)
	defer cancel()
	err := s.backend.Stop(ctx, inst)
	if err == nil {
		s.log.Info("экземпляр остановлен", "step", inst.Step, "agent_id", inst.ID)
	}
	return err
}

func (s *Scaler) announce(ctx context.Context, action, step string, backlog, was, now int) {
	s.log.Info(action, "step", step, "backlog", backlog, "was", was, "now", now)
	s.emit.Emit(ctx, core.Event{Action: action, Source: eventSource, Step: step,
		Summary: fmt.Sprintf("%s: backlog=%d, было %d → стало %d", step, backlog, was, now)})
}

// Shutdown останавливает все добавочные экземпляры, которыми управляет автомасштабировщик.
// ctx должен быть живым (не отменённым сигналом).
func (s *Scaler) Shutdown(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, shutdownWait)
	defer cancel()
	var wg sync.WaitGroup
	for _, sc := range s.steps {
		extras, err := s.backend.List(ctx, sc.Step)
		if err != nil {
			s.log.Error("остановка: список экземпляров недоступен", "step", sc.Step, "err", err)
			continue
		}
		for _, inst := range extras {
			wg.Add(1)
			go func() {
				defer wg.Done()
				if err := s.stopOne(ctx, inst); err != nil {
					s.log.Error("остановка экземпляра не удалась", "agent_id", inst.ID, "err", err)
				}
			}()
		}
		if len(extras) > 0 {
			s.emit.Emit(ctx, core.Event{Action: "scaled_down", Source: eventSource, Step: sc.Step,
				Summary: fmt.Sprintf("%s: остановка автомасштабировщика, было %d → стало %d",
					sc.Step, sc.Min+len(extras), sc.Min)})
		}
	}
	wg.Wait()
}

func (s *Scaler) setStatus(st *stepState, f func(*StepStatus)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f(&st.status)
}
