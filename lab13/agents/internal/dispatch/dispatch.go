// Пакет dispatch — общий диспетчер отложенных отправок на Redis: zset «когда отправить»
// + hash с данными. Используется напоминаниями (mas:reminders:due) и опросами
// (mas:surveys:due), чтобы не дублировать код.
package dispatch

import (
	"context"
	"log/slog"
	"math"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

const (
	NotifySubject = "notify.patient"
	retryDelay    = 5 * time.Second
	batchSize     = 100
	traceField    = "traceparent"
)

// Deliver выполняет отправку; ошибка возвращает элемент в очередь с задержкой.
type Deliver func(ctx context.Context, id string, fields map[string]string) error

type Config struct {
	DueKey     string // zset: id -> unix-время отправки
	ItemPrefix string // hash элемента: ItemPrefix + id
	StatsKey   string // hash счётчиков, поле sent
	Interval   time.Duration
}

type Dispatcher struct {
	cfg     Config
	st      store.Store
	clock   core.Clock
	deliver Deliver
	log     *slog.Logger
}

func New(cfg Config, st store.Store, clock core.Clock, deliver Deliver, log *slog.Logger) *Dispatcher {
	if cfg.Interval <= 0 {
		cfg.Interval = time.Second
	}
	return &Dispatcher{cfg: cfg, st: st, clock: clock, deliver: deliver, log: log}
}

func score(t time.Time) float64 { return float64(t.UnixMilli()) / 1000 }

// Schedule сохраняет данные и ставит элемент в очередь. Контекст трассировки
// запоминается в hash, чтобы отправка через часы осталась в той же трассе.
//
// Повторный вызов с тем же id ничего не меняет: оркестратор может повторить задачу после
// таймаута, а JetStream всё равно доставит и старую попытку — без этой проверки пациент
// получил бы уведомление дважды (в том числе если первое уже отправлено и снято с очереди).
func (d *Dispatcher) Schedule(ctx context.Context, id string, at time.Time, fields map[string]string) error {
	first, err := d.st.SetNX(ctx, d.cfg.ItemPrefix+id+":once", "1")
	if err != nil {
		return err
	}
	if !first {
		return nil
	}
	rec := make(map[string]string, len(fields)+3)
	for k, v := range fields {
		rec[k] = v
	}
	rec["status"] = "scheduled"
	rec["send_at"] = at.UTC().Format(time.RFC3339)
	mc := propagation.MapCarrier{}
	propagation.TraceContext{}.Inject(ctx, mc)
	if tp := mc.Get(traceField); tp != "" {
		rec[traceField] = tp
	}
	// При сбое записи маркер снимается: иначе повтор задачи увидел бы «уже запланировано»
	// и напоминание пропало бы без следа (находка №14).
	rollback := func(err error) error {
		_ = d.st.Del(ctx, d.cfg.ItemPrefix+id+":once", d.cfg.ItemPrefix+id)
		return err
	}
	if err := d.st.HSet(ctx, d.cfg.ItemPrefix+id, rec); err != nil {
		return rollback(err)
	}
	if err := d.st.ZAdd(ctx, d.cfg.DueKey, score(at), id); err != nil {
		return rollback(err)
	}
	return nil
}

// Tick забирает созревшие элементы и отправляет их; возвращает число отправленных.
// «Забирает» тот экземпляр, чей ZREM вернул true, — так дубли между экземплярами исключены.
func (d *Dispatcher) Tick(ctx context.Context) (int, error) {
	ids, err := d.st.ZRangeByScore(ctx, d.cfg.DueKey, math.Inf(-1), score(d.clock.Now()), batchSize)
	if err != nil {
		return 0, err
	}
	sent := 0
	for _, id := range ids {
		taken, err := d.st.ZRem(ctx, d.cfg.DueKey, id)
		if err != nil {
			return sent, err
		}
		if !taken {
			continue
		}
		fields, err := d.st.HGetAll(ctx, d.cfg.ItemPrefix+id)
		if err != nil || len(fields) == 0 {
			d.log.Warn("данные элемента недоступны", "id", id, "err", err)
			if err != nil {
				d.requeue(ctx, id)
			}
			continue
		}
		ictx, span := d.itemContext(ctx, fields)
		err = d.deliver(ictx, id, fields)
		span.End()
		if err != nil {
			d.log.Warn("отправка не удалась, повтор позже", "id", id, "err", err)
			d.requeue(ctx, id)
			continue
		}
		_ = d.st.HSet(ctx, d.cfg.ItemPrefix+id, map[string]string{
			"status": "sent", "sent_at": d.clock.Now().Format(time.RFC3339),
		})
		if _, err := d.st.HIncrBy(ctx, d.cfg.StatsKey, "sent", 1); err != nil {
			d.log.Warn("не удалось увеличить sent", "err", err)
		}
		sent++
	}
	return sent, nil
}

func (d *Dispatcher) requeue(ctx context.Context, id string) {
	if err := d.st.ZAdd(ctx, d.cfg.DueKey, score(d.clock.Now().Add(retryDelay)), id); err != nil {
		d.log.Error("элемент потерян: не удалось вернуть в очередь", "id", id, "err", err)
	}
}

func (d *Dispatcher) itemContext(ctx context.Context, fields map[string]string) (context.Context, trace.Span) {
	if tp := fields[traceField]; tp != "" {
		ctx = propagation.TraceContext{}.Extract(ctx, propagation.MapCarrier{traceField: tp})
	}
	return otel.Tracer("agents/dispatch").Start(ctx, "dispatch "+d.cfg.ItemPrefix, trace.WithSpanKind(trace.SpanKindProducer))
}

// Run — цикл диспетчера до отмены ctx.
func (d *Dispatcher) Run(ctx context.Context) {
	t := time.NewTicker(d.cfg.Interval)
	defer t.Stop()
	for {
		if _, err := d.Tick(ctx); err != nil && ctx.Err() == nil {
			d.log.Warn("диспетчер: ошибка тика", "err", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// NotifyDeliver строит Deliver, публикующий notify.patient и событие notified.
// kind — "reminder" или "survey".
func NotifyDeliver(sink core.Sink, clock core.Clock, kind string) Deliver {
	return func(ctx context.Context, id string, fields map[string]string) error {
		msg := map[string]any{
			"type": kind, "id": id, "sent_at": clock.Now().Format(time.RFC3339),
		}
		for _, k := range []string{"run_id", "appointment_id", "patient_id", "patient_name", "phone", "channel", "text", "send_at"} {
			if v, ok := fields[k]; ok {
				msg[k] = v
			}
		}
		if err := sink.Publish(ctx, NotifySubject, msg); err != nil {
			return err
		}
		sink.Emit(ctx, core.Event{
			Action: "notified", RunID: fields["run_id"], Subject: NotifySubject,
			Summary: kind + " " + id + " -> " + fields["patient_id"] + " (" + fields["channel"] + ")",
		})
		return nil
	}
}
