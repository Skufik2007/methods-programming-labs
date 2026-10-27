// Пакет bus — всё, что связано с NATS: подключение, поток TASKS, консьюмер, цикл
// обработки задач, heartbeat, события, распространение traceparent.
package bus

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/stats"
)

const (
	StreamName        = "TASKS"
	HeartbeatSubject  = "agents.heartbeat"
	HeartbeatInterval = 5 * time.Second
	AckWait           = 30 * time.Second
	MaxDeliver        = 5
	DrainTimeout      = 10 * time.Second
	tracerName        = "agents/bus"
)

// Info — статические сведения об агенте для heartbeat и событий.
type Info struct {
	AgentID   string
	Step      string
	Kind      string
	Title     string
	StartedAt time.Time
}

// Chaos — демонстрационные сбои.
type Chaos struct {
	FailRate float64
	Slow     time.Duration
}

type Bus struct {
	nc       *nats.Conn
	js       jetstream.JetStream
	info     Info
	clock    core.Clock
	log      *slog.Logger
	counters *stats.Counters
	prop     propagation.TextMapPropagator
	inflight atomic.Int64
}

// Connect подключается к NATS, повторяя попытки (при старте compose NATS может подняться позже).
func Connect(ctx context.Context, url string, info Info, clock core.Clock, log *slog.Logger, counters *stats.Counters) (*Bus, error) {
	var (
		nc  *nats.Conn
		err error
	)
	for attempt := 1; attempt <= 10; attempt++ {
		nc, err = nats.Connect(url, nats.Name(info.AgentID), nats.MaxReconnects(-1),
			nats.ReconnectWait(time.Second), nats.Timeout(3*time.Second))
		if err == nil {
			break
		}
		log.Warn("NATS недоступен, повтор", "attempt", attempt, "err", err)
		select {
		case <-time.After(time.Second):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if err != nil {
		return nil, fmt.Errorf("подключение к NATS %s: %w", url, err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, err
	}
	return &Bus{nc: nc, js: js, info: info, clock: clock, log: log, counters: counters,
		prop: propagation.TraceContext{}}, nil
}

// Close дожидается отправки буферов и закрывает соединение.
func (b *Bus) Close() {
	if err := b.nc.Drain(); err != nil {
		b.nc.Close()
	}
}

// --- W3C traceparent в заголовках NATS ---

// carrier не использует propagation.HeaderCarrier: тот канонизирует ключи
// («Traceparent»), а клиенты (в т.ч. Python) шлют «traceparent» как есть.
type carrier nats.Header

func (c carrier) Get(key string) string {
	for k, v := range c {
		if strings.EqualFold(k, key) && len(v) > 0 {
			return v[0]
		}
	}
	return ""
}

func (c carrier) Set(key, value string) { c[strings.ToLower(key)] = []string{value} }

func (c carrier) Keys() []string {
	keys := make([]string, 0, len(c))
	for k := range c {
		keys = append(keys, k)
	}
	return keys
}

// --- core.Sink ---

func (b *Bus) Publish(ctx context.Context, subject string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	msg := &nats.Msg{Subject: subject, Data: data, Header: nats.Header{}}
	b.prop.Inject(ctx, carrier(msg.Header))
	return b.nc.PublishMsg(msg)
}

func (b *Bus) Emit(ctx context.Context, e core.Event) {
	if e.TS == "" {
		e.TS = b.clock.Now().Format(time.RFC3339)
	}
	if e.Source == "" {
		e.Source = b.info.AgentID
	}
	if e.Step == "" {
		e.Step = b.info.Step
	}
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		e.TraceID, e.SpanID = sc.TraceID().String(), sc.SpanID().String()
	}
	data, err := json.Marshal(e)
	if err == nil {
		err = b.nc.Publish("events."+e.Source, data)
	}
	if err != nil {
		b.log.Debug("не удалось опубликовать событие", "action", e.Action, "err", err)
	}
}

// --- heartbeat ---

type heartbeat struct {
	AgentID   string `json:"agent_id"`
	Role      string `json:"role"`
	Kind      string `json:"kind"`
	Title     string `json:"title"`
	Status    string `json:"status"`
	Processed int64  `json:"processed"`
	Failed    int64  `json:"failed"`
	Inflight  int64  `json:"inflight"`
	StartedAt string `json:"started_at"`
	SentAt    string `json:"sent_at"`
	Host      string `json:"host"`
	Lang      string `json:"lang"`
}

func (b *Bus) SendHeartbeat(status string) {
	hb := heartbeat{
		AgentID: b.info.AgentID, Role: b.info.Step, Kind: b.info.Kind, Title: b.info.Title,
		Status: status, Processed: b.counters.Processed(), Failed: b.counters.Failed(),
		Inflight: b.inflight.Load(), StartedAt: b.info.StartedAt.UTC().Format(time.RFC3339),
		SentAt: b.clock.Now().Format(time.RFC3339), Host: fmt.Sprintf("pid-%d", os.Getpid()), Lang: "go",
	}
	data, _ := json.Marshal(hb)
	if err := b.nc.Publish(HeartbeatSubject, data); err != nil {
		b.log.Debug("heartbeat не отправлен", "err", err)
	}
}

// RunHeartbeat шлёт heartbeat «ready» раз в 5 с до отмены ctx.
func (b *Bus) RunHeartbeat(ctx context.Context) {
	t := time.NewTicker(HeartbeatInterval)
	defer t.Stop()
	b.SendHeartbeat("ready")
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			b.SendHeartbeat("ready")
		}
	}
}

// --- подписки core NATS ---

// Subscribe подписывается и дожидается, пока сервер зарегистрирует подписку (Flush):
// без этого агент считается готовым раньше, чем начнёт получать аукционы, и первая ставка
// теряется (находка №15: под -race в CI второй агент записи не успевал подписаться).
func (b *Bus) Subscribe(s core.Subscription) (*nats.Subscription, error) {
	sub, err := b.subscribe(s)
	if err != nil {
		return nil, err
	}
	if err := b.nc.Flush(); err != nil {
		_ = sub.Unsubscribe()
		return nil, fmt.Errorf("подписка %s: %w", s.Subject, err)
	}
	return sub, nil
}

func (b *Bus) subscribe(s core.Subscription) (*nats.Subscription, error) {
	return b.nc.Subscribe(s.Subject, func(m *nats.Msg) {
		ctx := b.prop.Extract(context.Background(), carrier(m.Header))
		ctx, span := otel.Tracer(tracerName).Start(ctx, s.Span, trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(attribute.String("messaging.destination.name", m.Subject)))
		defer span.End()
		reply, failed := s.Handle(ctx, m.Data)
		if failed {
			span.SetStatus(codes.Error, "обработка завершилась ошибкой")
		}
		b.counters.Done(ctx, failed)
		if reply != nil && m.Reply != "" {
			if err := m.Respond(reply); err != nil {
				b.log.Warn("не удалось ответить", "subject", m.Subject, "err", err)
			}
		}
	})
}

// --- поток TASKS и обработка задач ---

// EnsureConsumer идемпотентно создаёт поток TASKS и консьюмер step-<step>.
func (b *Bus) EnsureConsumer(ctx context.Context, step string) (jetstream.Consumer, error) {
	_, err := b.js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:      StreamName,
		Subjects:  []string{"tasks.>"},
		Retention: jetstream.WorkQueuePolicy,
		Storage:   jetstream.FileStorage,
		MaxAge:    24 * time.Hour,
	})
	if err != nil {
		return nil, fmt.Errorf("поток %s: %w", StreamName, err)
	}
	cons, err := b.js.CreateOrUpdateConsumer(ctx, StreamName, jetstream.ConsumerConfig{
		Durable:       "step-" + step,
		FilterSubject: "tasks." + step,
		AckPolicy:     jetstream.AckExplicitPolicy,
		AckWait:       AckWait,
		MaxDeliver:    MaxDeliver,
	})
	if err != nil {
		return nil, fmt.Errorf("консьюмер step-%s: %w", step, err)
	}
	return cons, nil
}

// RunTasks выбирает задачи, пока ctx жив, не более concurrency одновременно. После отмены
// ctx прекращает выборку и ждёт текущие задачи до DrainTimeout.
func (b *Bus) RunTasks(ctx context.Context, cons jetstream.Consumer, h core.TaskHandler, concurrency int, ch Chaos) {
	if concurrency < 1 {
		concurrency = 1
	}
	sem := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	// Текущие задачи доводим до конца, даже если пришёл сигнал остановки.
	workCtx := context.WithoutCancel(ctx)

loop:
	for ctx.Err() == nil {
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			break loop
		}
		n := 1
	grab:
		for n < concurrency {
			select {
			case sem <- struct{}{}:
				n++
			default:
				break grab
			}
		}

		batch, err := cons.Fetch(n, jetstream.FetchMaxWait(time.Second))
		if err != nil {
			for i := 0; i < n; i++ {
				<-sem
			}
			b.log.Warn("ошибка выборки задач", "err", err)
			select {
			case <-time.After(time.Second):
			case <-ctx.Done():
			}
			continue
		}
		got := 0
		for msg := range batch.Messages() {
			got++
			wg.Add(1)
			b.inflight.Add(1)
			go func() {
				defer func() {
					b.inflight.Add(-1)
					<-sem
					wg.Done()
				}()
				b.handleMsg(workCtx, msg, h, ch)
			}()
		}
		if err := batch.Error(); err != nil && !errors.Is(err, nats.ErrTimeout) {
			b.log.Debug("выборка завершилась с ошибкой", "err", err)
		}
		for i := got; i < n; i++ {
			<-sem
		}
	}

	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(DrainTimeout):
		b.log.Warn("не все задачи завершились за время остановки; они вернутся в очередь", "inflight", b.inflight.Load())
	}
}

func (b *Bus) handleMsg(ctx context.Context, msg jetstream.Msg, h core.TaskHandler, ch Chaos) {
	started := b.clock.Now()
	ctx = b.prop.Extract(ctx, carrier(msg.Headers()))
	ctx, span := otel.Tracer(tracerName).Start(ctx, "process "+b.info.Step, trace.WithSpanKind(trace.SpanKindConsumer),
		trace.WithAttributes(attribute.String("messaging.destination.name", msg.Subject()),
			attribute.String("agent.id", b.info.AgentID)))
	defer span.End()

	var task core.Task
	if err := json.Unmarshal(msg.Data(), &task); err != nil || task.RunID == "" {
		reason := "отсутствует run_id"
		if err != nil {
			reason = "некорректный JSON: " + err.Error()
		}
		// Term: повторная доставка испорченного сообщения бессмысленна.
		if terr := msg.Term(); terr != nil {
			b.log.Warn("Term не удался", "err", terr)
		}
		span.SetStatus(codes.Error, reason)
		b.Emit(ctx, core.Event{Action: "failed", Subject: msg.Subject(), Summary: reason})
		b.counters.Done(ctx, true)
		b.log.Warn("задача отброшена", "reason", reason)
		return
	}
	if task.Step == "" {
		task.Step = b.info.Step
	}
	span.SetAttributes(attribute.String("run.id", task.RunID), attribute.String("task.id", task.TaskID))
	ev := func(action, summary string) {
		b.Emit(ctx, core.Event{Action: action, RunID: task.RunID, TaskID: task.TaskID, Step: task.Step,
			Subject: msg.Subject(), Summary: summary})
	}
	ev("received", fmt.Sprintf("kind=%s attempt=%d", task.Kind, task.Attempt))

	if ch.Slow > 0 {
		select {
		case <-time.After(ch.Slow):
		case <-ctx.Done():
		}
	}

	var (
		output any
		herr   error
	)
	if ch.FailRate > 0 && rand.Float64() < ch.FailRate {
		herr = core.Transient("simulated failure")
	} else {
		output, herr = safeHandle(ctx, h, task)
	}

	res := core.Result{
		TaskID: task.TaskID, RunID: task.RunID, Step: task.Step, AgentID: b.info.AgentID,
		Success: herr == nil, Output: output,
	}
	if herr != nil {
		res.Output = map[string]any{}
		res.Error = herr.Error()
		res.Retryable = core.IsRetryable(herr)
		span.RecordError(herr)
		span.SetStatus(codes.Error, herr.Error())
	}
	res.DurationMS = b.clock.Now().Sub(started).Milliseconds()
	res.FinishedAt = b.clock.Now().Format(time.RFC3339)

	if err := b.Publish(ctx, "results."+task.RunID, res); err != nil {
		// Результат не ушёл — не ack-аем, пусть JetStream доставит задачу снова.
		b.log.Error("не удалось опубликовать результат", "run_id", task.RunID, "err", err)
		_ = msg.NakWithDelay(2 * time.Second)
		ev("failed", "результат не опубликован: "+err.Error())
		return
	}
	ev("published", "results."+task.RunID)
	if err := msg.Ack(); err != nil {
		b.log.Warn("ack не удался", "err", err)
	}
	if herr != nil {
		ev("failed", herr.Error())
	} else {
		ev("completed", summarize(output))
	}
	b.counters.Done(ctx, herr != nil)
}

func safeHandle(ctx context.Context, h core.TaskHandler, t core.Task) (out any, err error) {
	defer func() {
		if r := recover(); r != nil {
			out, err = nil, core.Transient("паника обработчика: %v", r)
		}
	}()
	return h.Handle(ctx, t)
}

// summarize — короткая сводка для журнала: ключевые поля выхода, без персональных данных.
func summarize(output any) string {
	data, err := json.Marshal(output)
	if err != nil {
		return ""
	}
	var m map[string]any
	if json.Unmarshal(data, &m) != nil {
		return ""
	}
	var parts []string
	for _, k := range []string{"urgency", "specialty", "sentiment", "doctor_id"} {
		if v, ok := m[k]; ok && v != "" {
			parts = append(parts, fmt.Sprintf("%s=%v", k, v))
		}
	}
	if r, ok := m["reminders"].([]any); ok {
		parts = append(parts, fmt.Sprintf("reminders=%d", len(r)))
	}
	return strings.Join(parts, " ")
}
