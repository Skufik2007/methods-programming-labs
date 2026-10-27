package dispatch

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

const (
	dueKey     = "test:due"
	itemPrefix = "test:item:"
	statsKey   = "test:stats"
)

var base = time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)

// fakeClock — управляемые часы: Now возвращает последнее установленное значение.
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Set(t time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = t
}

func (c *fakeClock) clock() core.Clock { return func() time.Time { return c.Now() } }

func silentLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func newDisp(st store.Store, clk *fakeClock, deliver Deliver) *Dispatcher {
	return New(Config{DueKey: dueKey, ItemPrefix: itemPrefix, StatsKey: statsKey}, st, clk.clock(), deliver, silentLog())
}

// recorder запоминает отправки.
type recorder struct {
	mu       sync.Mutex
	sent     map[string]int
	fail     map[string]int // сколько раз вернуть ошибку для id
	traceIDs map[string]string
}

func newRecorder() *recorder {
	return &recorder{sent: map[string]int{}, fail: map[string]int{}, traceIDs: map[string]string{}}
}

func (r *recorder) deliver(ctx context.Context, id string, _ map[string]string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.fail[id] > 0 {
		r.fail[id]--
		return errors.New("канал недоступен")
	}
	r.sent[id]++
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.traceIDs[id] = sc.TraceID().String()
	}
	return nil
}

func (r *recorder) count(id string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sent[id]
}

func (r *recorder) total() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, v := range r.sent {
		n += v
	}
	return n
}

func TestScheduleAndTickDeliversOnlyWhenDue(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	d := newDisp(st, clk, rec.deliver)
	ctx := context.Background()

	if err := d.Schedule(ctx, "r-1", base.Add(time.Hour), map[string]string{"patient_id": "p-1", "channel": "sms"}); err != nil {
		t.Fatal(err)
	}
	if n, err := d.Tick(ctx); err != nil || n != 0 {
		t.Fatalf("до срока отправок быть не должно: n=%d err=%v", n, err)
	}

	clk.Set(base.Add(time.Hour))
	n, err := d.Tick(ctx)
	if err != nil || n != 1 || rec.count("r-1") != 1 {
		t.Fatalf("в срок: n=%d err=%v sent=%d", n, err, rec.count("r-1"))
	}
	m, _ := st.HGetAll(ctx, itemPrefix+"r-1")
	if m["status"] != "sent" || m["sent_at"] == "" {
		t.Errorf("статус элемента: %v", m)
	}
	stats, _ := st.HGetAll(ctx, statsKey)
	if stats["sent"] != "1" {
		t.Errorf("счётчик sent = %q", stats["sent"])
	}

	// Повторный тик не отправляет снова.
	if n, _ := d.Tick(ctx); n != 0 || rec.count("r-1") != 1 {
		t.Fatalf("повторный тик отправил снова: n=%d", n)
	}
}

// Повторный вызов Schedule с тем же id не создаёт дубля, даже после отправки.
func TestScheduleIsIdempotent(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	d := newDisp(st, clk, rec.deliver)
	ctx := context.Background()

	at := base.Add(time.Minute)
	for i := 0; i < 3; i++ {
		if err := d.Schedule(ctx, "dup", at, map[string]string{"x": "y"}); err != nil {
			t.Fatal(err)
		}
	}
	clk.Set(at)
	if n, _ := d.Tick(ctx); n != 1 {
		t.Fatalf("после трёх Schedule с одним id отправок %d, ожидалось 1", n)
	}
	// Повтор после отправки: элемент уже снят с очереди и не возвращается.
	if err := d.Schedule(ctx, "dup", at, map[string]string{"x": "y"}); err != nil {
		t.Fatal(err)
	}
	clk.Set(at.Add(time.Hour))
	if n, _ := d.Tick(ctx); n != 0 || rec.count("dup") != 1 {
		t.Fatalf("повтор после отправки дал дубль: n=%d sent=%d", n, rec.count("dup"))
	}
}

// Отправка с ошибкой возвращается в очередь через retryDelay и уходит при следующем тике.
func TestDeliveryFailureRequeues(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	rec.fail["f-1"] = 1
	d := newDisp(st, clk, rec.deliver)
	ctx := context.Background()
	_ = d.Schedule(ctx, "f-1", base, nil)

	if n, err := d.Tick(ctx); err != nil || n != 0 {
		t.Fatalf("первая попытка: n=%d err=%v", n, err)
	}
	// Сразу после неудачи — ещё не время повтора.
	clk.Set(base.Add(retryDelay - time.Second))
	if n, _ := d.Tick(ctx); n != 0 {
		t.Fatalf("повтор раньше retryDelay")
	}
	clk.Set(base.Add(retryDelay))
	if n, _ := d.Tick(ctx); n != 1 || rec.count("f-1") != 1 {
		t.Fatalf("повтор после retryDelay: n=%d sent=%d", n, rec.count("f-1"))
	}
}

// Трассировка: контекст из Schedule сохраняется в hash и восстанавливается при отправке.
func TestTraceContextSurvivesDelay(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	d := newDisp(st, clk, rec.deliver)

	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    trace.TraceID{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
		SpanID:     trace.SpanID{1, 2, 3, 4, 5, 6, 7, 8},
		TraceFlags: trace.FlagsSampled,
	})
	ctx := trace.ContextWithSpanContext(context.Background(), sc)
	if err := d.Schedule(ctx, "t-1", base, nil); err != nil {
		t.Fatal(err)
	}
	m, _ := st.HGetAll(context.Background(), itemPrefix+"t-1")
	if m["traceparent"] == "" {
		t.Fatal("traceparent не сохранён в записи")
	}
	if _, err := d.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	if rec.traceIDs["t-1"] != sc.TraceID().String() {
		t.Fatalf("trace_id при отправке %q, ожидался %q", rec.traceIDs["t-1"], sc.TraceID().String())
	}
}

// НАХОДКА: Schedule ставит маркер «уже запланировано» (SetNX ...:once) ДО записи данных и
// ZADD. Если HSET или ZADD падают, повтор задачи (оркестратор или JetStream) видит маркер,
// возвращает nil и ничего не ставит в очередь — напоминание теряется навсегда.
// Ожидание: после восстановления Redis повторный Schedule ставит элемент в очередь.
func TestScheduleRetryAfterPartialFailureNotLost(t *testing.T) {
	t.Parallel()
	fs := &flakyStore{Store: store.NewMemory(), failHSetOnce: true}
	clk := &fakeClock{now: base}
	rec := newRecorder()
	d := newDisp(fs, clk, rec.deliver)
	ctx := context.Background()

	if err := d.Schedule(ctx, "lost-1", base, nil); err == nil {
		t.Fatal("первый вызов должен вернуть ошибку HSET")
	}
	// Повтор той же задачи после восстановления Redis.
	if err := d.Schedule(ctx, "lost-1", base, nil); err != nil {
		t.Fatalf("повтор: %v", err)
	}
	if n, _ := d.Tick(ctx); n != 1 {
		t.Fatalf("НАХОДКА: после повтора Schedule напоминание не поставлено в очередь (отправок %d, ожидалось 1)", n)
	}
}

// flakyStore: первый HSet падает, остальные операции проходят.
type flakyStore struct {
	store.Store
	mu           sync.Mutex
	failHSetOnce bool
}

func (f *flakyStore) HSet(ctx context.Context, key string, v map[string]string) error {
	f.mu.Lock()
	fail := f.failHSetOnce
	f.failHSetOnce = false
	f.mu.Unlock()
	if fail {
		return errors.New("сеть")
	}
	return f.Store.HSet(ctx, key, v)
}

// Два диспетчера на одном хранилище: каждый элемент отправляется ровно один раз.
func TestTwoDispatchersNoDuplicates(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	d1 := newDisp(st, clk, rec.deliver)
	d2 := newDisp(st, clk, rec.deliver)
	ctx := context.Background()

	const items = 60
	for i := 0; i < items; i++ {
		id := string(rune('A'+i/26)) + string(rune('a'+i%26))
		if err := d1.Schedule(ctx, id, base, nil); err != nil {
			t.Fatal(err)
		}
	}
	clk.Set(base.Add(time.Second))

	var wg sync.WaitGroup
	for _, d := range []*Dispatcher{d1, d2} {
		wg.Add(1)
		go func(d *Dispatcher) {
			defer wg.Done()
			for k := 0; k < 20; k++ {
				_, _ = d.Tick(ctx)
			}
		}(d)
	}
	wg.Wait()

	if got := rec.total(); got != items {
		t.Fatalf("всего отправок %d, ожидалось %d", got, items)
	}
	for id, n := range rec.sent {
		if n != 1 {
			t.Fatalf("элемент %s отправлен %d раз", id, n)
		}
	}
}

func TestTickSkipsItemWithoutData(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	d := newDisp(st, clk, rec.deliver)
	ctx := context.Background()
	// В очереди есть id, а данных нет (например, hash удалён вручную).
	if err := st.ZAdd(ctx, dueKey, float64(base.Unix()), "orphan"); err != nil {
		t.Fatal(err)
	}
	if n, err := d.Tick(ctx); err != nil || n != 0 {
		t.Fatalf("сирота: n=%d err=%v", n, err)
	}
	if rec.total() != 0 {
		t.Fatal("сирота не должна отправляться")
	}
}

func TestTickStoreErrors(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	clk := &fakeClock{now: base}

	t.Run("ошибка чтения очереди", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory(), failZRange: true}
		d := newDisp(fs, clk, newRecorder().deliver)
		if _, err := d.Tick(ctx); err == nil {
			t.Fatal("ожидалась ошибка ZRANGEBYSCORE")
		}
	})

	t.Run("ошибка ZREM прерывает тик", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory(), failZRem: true}
		_ = fs.ZAdd(ctx, dueKey, float64(base.Unix()), "x")
		d := newDisp(fs, clk, newRecorder().deliver)
		if _, err := d.Tick(ctx); err == nil {
			t.Fatal("ожидалась ошибка ZREM")
		}
	})

	t.Run("ошибка чтения данных: элемент возвращается в очередь", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory(), failHGetAll: true}
		_ = fs.ZAdd(ctx, dueKey, float64(base.Unix()), "y")
		rec := newRecorder()
		d := newDisp(fs, clk, rec.deliver)
		if n, err := d.Tick(ctx); err != nil || n != 0 {
			t.Fatalf("n=%d err=%v", n, err)
		}
		// requeue: элемент снова в очереди с задержкой.
		fs.failHGetAll = false
		clk.Set(base.Add(retryDelay))
		_ = fs.HSet(ctx, itemPrefix+"y", map[string]string{"x": "1"})
		if n, _ := d.Tick(ctx); n != 1 {
			t.Fatalf("после восстановления элемент должен уйти: %d", n)
		}
	})

	t.Run("ошибка статистики не мешает отправке", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory(), failHIncr: true}
		rec := newRecorder()
		d := newDisp(fs, clk, rec.deliver)
		_ = d.Schedule(ctx, "s", base, nil)
		if n, err := d.Tick(ctx); err != nil || n != 1 {
			t.Fatalf("n=%d err=%v", n, err)
		}
	})

	t.Run("ошибка SETNX при постановке", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory(), failSetNX: true}
		d := newDisp(fs, clk, newRecorder().deliver)
		if err := d.Schedule(ctx, "z", base, nil); err == nil {
			t.Fatal("ожидалась ошибка SetNX")
		}
	})

	t.Run("ошибка ZADD при постановке", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory(), failZAdd: true}
		d := newDisp(fs, clk, newRecorder().deliver)
		if err := d.Schedule(ctx, "w", base, nil); err == nil {
			t.Fatal("ожидалась ошибка ZADD")
		}
	})

	t.Run("ошибка requeue при неудачной отправке логируется", func(t *testing.T) {
		t.Parallel()
		fs := &errStore{Store: store.NewMemory()}
		rec := newRecorder()
		rec.fail["q"] = 1
		d := newDisp(fs, clk, rec.deliver)
		_ = d.Schedule(ctx, "q", base, nil)
		fs.failZAdd = true
		if n, err := d.Tick(ctx); err != nil || n != 0 {
			t.Fatalf("n=%d err=%v", n, err)
		}
	})
}

// errStore — хранилище с точечными сбоями.
type errStore struct {
	store.Store
	failZRange, failZRem, failHGetAll, failHIncr, failSetNX, failZAdd bool
}

func (e *errStore) ZRangeByScore(ctx context.Context, k string, lo, hi float64, n int64) ([]string, error) {
	if e.failZRange {
		return nil, errors.New("сеть")
	}
	return e.Store.ZRangeByScore(ctx, k, lo, hi, n)
}

func (e *errStore) ZRem(ctx context.Context, k, m string) (bool, error) {
	if e.failZRem {
		return false, errors.New("сеть")
	}
	return e.Store.ZRem(ctx, k, m)
}

func (e *errStore) HGetAll(ctx context.Context, k string) (map[string]string, error) {
	if e.failHGetAll {
		return nil, errors.New("сеть")
	}
	return e.Store.HGetAll(ctx, k)
}

func (e *errStore) HIncrBy(ctx context.Context, k, f string, n int64) (int64, error) {
	if e.failHIncr {
		return 0, errors.New("сеть")
	}
	return e.Store.HIncrBy(ctx, k, f, n)
}

func (e *errStore) SetNX(ctx context.Context, k, v string) (bool, error) {
	if e.failSetNX {
		return false, errors.New("сеть")
	}
	return e.Store.SetNX(ctx, k, v)
}

func (e *errStore) ZAdd(ctx context.Context, k string, s float64, m string) error {
	if e.failZAdd {
		return errors.New("сеть")
	}
	return e.Store.ZAdd(ctx, k, s, m)
}

func TestRunStopsOnCancel(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	clk := &fakeClock{now: base}
	rec := newRecorder()
	cfg := Config{DueKey: dueKey, ItemPrefix: itemPrefix, StatsKey: statsKey, Interval: 5 * time.Millisecond}
	d := New(cfg, st, clk.clock(), rec.deliver, silentLog())
	_ = d.Schedule(context.Background(), "run-1", base, nil)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { d.Run(ctx); close(done) }()

	deadline := time.Now().Add(2 * time.Second)
	for rec.count("run-1") == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run не остановился после отмены контекста")
	}
	if rec.count("run-1") != 1 {
		t.Fatalf("Run не отправил элемент: %d", rec.count("run-1"))
	}
}

func TestNewDefaultInterval(t *testing.T) {
	t.Parallel()
	d := New(Config{DueKey: dueKey, ItemPrefix: itemPrefix}, store.NewMemory(), (&fakeClock{}).clock(), nil, silentLog())
	if d.cfg.Interval != time.Second {
		t.Fatalf("Interval по умолчанию = %v", d.cfg.Interval)
	}
}

// recSink — запоминает публикации и события.
type recSink struct {
	mu        sync.Mutex
	published []string
	events    []core.Event
	failPub   bool
}

func (r *recSink) Emit(_ context.Context, e core.Event) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, e)
}

func (r *recSink) Publish(_ context.Context, subject string, _ any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.failPub {
		return errors.New("сеть")
	}
	r.published = append(r.published, subject)
	return nil
}

func TestNotifyDeliver(t *testing.T) {
	t.Parallel()
	sink := &recSink{}
	clk := &fakeClock{now: base}
	deliver := NotifyDeliver(sink, clk.clock(), "reminder")
	fields := map[string]string{"run_id": "r-1", "patient_id": "p-1", "channel": "sms", "text": "t",
		"secret_unused": "x"}
	if err := deliver(context.Background(), "id-1", fields); err != nil {
		t.Fatal(err)
	}
	if len(sink.published) != 1 || sink.published[0] != NotifySubject {
		t.Fatalf("публикации: %v", sink.published)
	}
	if len(sink.events) != 1 || sink.events[0].Action != "notified" || sink.events[0].RunID != "r-1" {
		t.Fatalf("события: %+v", sink.events)
	}
	sink.failPub = true
	if err := deliver(context.Background(), "id-2", fields); err == nil {
		t.Fatal("ошибка публикации должна возвращаться (элемент уйдёт в повтор)")
	}
}

func TestNotifyDeliverDoesNotLeakUnknownFields(t *testing.T) {
	t.Parallel()
	var captured map[string]any
	sink := &captureSink{capture: &captured}
	deliver := NotifyDeliver(sink, (&fakeClock{now: base}).clock(), "survey")
	_ = deliver(context.Background(), "s-1", map[string]string{"patient_id": "p", "traceparent": "tp", "channel": "sms"})
	if _, ok := captured["traceparent"]; ok {
		t.Error("служебные поля hash не должны уходить пациенту")
	}
	if captured["type"] != "survey" || captured["id"] != "s-1" {
		t.Errorf("сообщение: %v", captured)
	}
}

type captureSink struct {
	recSink
	capture *map[string]any
}

func (c *captureSink) Publish(_ context.Context, _ string, v any) error {
	m, _ := v.(map[string]any)
	*c.capture = m
	return nil
}
