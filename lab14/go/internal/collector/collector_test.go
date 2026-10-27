package collector

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/config"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks"
)

var t0 = time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)

func fp(v float64) *float64 { return &v }

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// recSink записывает события стока в общий журнал.
type recSink struct {
	mu        sync.Mutex
	name      string
	events    *journal
	rawCalls  [][]model.Reading
	failFlush int // сколько раз Flush вернёт ошибку
	closed    bool
}

func (s *recSink) WriteRaw(_ context.Context, b []model.Reading) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rawCalls = append(s.rawCalls, append([]model.Reading(nil), b...))
	s.events.add("write:" + s.name)
	return nil
}

func (s *recSink) WriteAgg(_ context.Context, b []model.Agg) error {
	s.events.add("writeagg:" + s.name)
	return nil
}

func (s *recSink) Flush(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failFlush > 0 {
		s.failFlush--
		s.events.add("flushfail:" + s.name)
		return errors.New("сток недоступен")
	}
	s.events.add("flush:" + s.name)
	return nil
}

func (s *recSink) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closed = true
	s.events.add("close:" + s.name)
	return nil
}

func (s *recSink) rawCount() (calls, records int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.rawCalls {
		records += len(c)
	}
	return len(s.rawCalls), records
}

// journal — упорядоченный журнал событий (потокобезопасный).
type journal struct {
	mu sync.Mutex
	ev []string
}

func (j *journal) add(s string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.ev = append(j.ev, s)
}

func (j *journal) list() []string {
	j.mu.Lock()
	defer j.mu.Unlock()
	return append([]string(nil), j.ev...)
}

func (j *journal) index(s string) int {
	for i, e := range j.list() {
		if e == s {
			return i
		}
	}
	return -1
}

// cpStore — чекпоинты с журналом сохранений.
type cpStore struct {
	*MemoryStore
	j *journal
}

func (s *cpStore) Save(ctx context.Context, seqs map[string]int64) error {
	s.j.add("checkpoint")
	return s.MemoryStore.Save(ctx, seqs)
}

// stubAsg — назначение фиксированного списка устройств.
type stubAsg struct {
	devs  []string
	j     *journal
	calls int
}

func (a *stubAsg) Devices(context.Context) ([]string, error) { return a.devs, nil }
func (a *stubAsg) Close(context.Context) error {
	a.j.add("asgclose")
	return nil
}

// stubSrc отдаёт заранее заданные показания, один раз на устройство.
type stubSrc struct {
	mu       sync.Mutex
	readings map[string][]model.Reading
}

func (s *stubSrc) Devices(context.Context) ([]string, error) { return nil, nil }

func (s *stubSrc) Readings(_ context.Context, dev string, after int64, limit int) (ReadingsResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []model.Reading
	for _, r := range s.readings[dev] {
		if r.Seq > after {
			out = append(out, r)
		}
	}
	last := after
	if len(out) > 0 {
		last = out[len(out)-1].Seq
	}
	s.readings[dev] = nil // отдано: повторно не выдаём
	return ReadingsResponse{DeviceID: dev, Readings: out, LastSeq: last}, nil
}

func baseConfig() config.Collector {
	return config.Collector{
		CollectorID: "c1", Workers: 1, PollInterval: time.Second, FetchLimit: 1000,
		Buffer: 100, BatchSize: 500, FlushInterval: 2 * time.Second,
		Window: 10 * time.Second, WindowLateness: 2 * time.Second, Emit: "raw",
		ShutdownTimeout: 5 * time.Second, StatusAddr: "",
	}
}

func reading(dev string, seq int64, ts time.Time) model.Reading {
	return model.Reading{DeviceID: dev, PatientID: "p", Seq: seq, Ts: ts, Metric: "heart_rate", Value: fp(70), Unit: "bpm"}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("таймаут: %s", what)
}

func TestBatchFlushBySize(t *testing.T) {
	cfg := baseConfig()
	cfg.BatchSize = 3
	j := &journal{}
	s := &recSink{name: "files", events: j}
	clk := clock.NewFake(t0)
	c, err := New(Deps{Config: cfg, Log: quietLog(), Clock: clk, Source: &stubSrc{}, Assigner: &stubAsg{j: j},
		Checkpoints: NewMemoryStore(), Sinks: []sinks.Named{{Name: "files", Sink: s}}})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- c.runPipeline() }()

	for seq := int64(1); seq <= 3; seq++ {
		c.in <- reading("dev-0001", seq, t0)
	}
	waitFor(t, "пакет из 3 записей", func() bool { _, n := s.rawCount(); return n == 3 })
	if calls, _ := s.rawCount(); calls != 1 {
		t.Fatalf("вызовов WriteRaw %d, ожидался 1 пакет", calls)
	}
	close(c.in)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestBatchFlushByTimer(t *testing.T) {
	cfg := baseConfig()
	cfg.BatchSize = 100 // порог размера не достигается
	j := &journal{}
	s := &recSink{name: "files", events: j}
	clk := clock.NewFake(t0)
	c, err := New(Deps{Config: cfg, Log: quietLog(), Clock: clk, Source: &stubSrc{}, Assigner: &stubAsg{j: j},
		Checkpoints: NewMemoryStore(), Sinks: []sinks.Named{{Name: "files", Sink: s}}})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- c.runPipeline() }()

	waitFor(t, "подписка таймеров", func() bool { return clk.Waiters() >= 2 })
	c.in <- reading("dev-0001", 1, t0)
	c.in <- reading("dev-0001", 2, t0)
	waitFor(t, "чтение канала", func() bool { return len(c.in) == 0 })
	time.Sleep(10 * time.Millisecond) // последнее показание уже обработано

	clk.Advance(time.Second) // до FLUSH_INTERVAL писать нечего
	if calls, _ := s.rawCount(); calls != 0 {
		t.Fatalf("запись до истечения FLUSH_INTERVAL: %d вызовов", calls)
	}
	clk.Advance(time.Second) // ровно 2 с — сброс по таймеру
	waitFor(t, "сброс по таймеру", func() bool { _, n := s.rawCount(); return n == 2 })

	close(c.in)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestCheckpointAdvancesOnlyAfterAllSinksFlush(t *testing.T) {
	cfg := baseConfig()
	cfg.BatchSize = 1000
	j := &journal{}
	good := &recSink{name: "files", events: j}
	bad := &recSink{name: "nats", events: j, failFlush: 1}
	cps := &cpStore{MemoryStore: NewMemoryStore(), j: j}
	clk := clock.NewFake(t0)
	c, err := New(Deps{Config: cfg, Log: quietLog(), Clock: clk, Source: &stubSrc{}, Assigner: &stubAsg{j: j},
		Checkpoints: cps, Sinks: []sinks.Named{{Name: "files", Sink: good}, {Name: "nats", Sink: bad}}})
	if err != nil {
		t.Fatal(err)
	}
	p := &pipeline{c: c, pending: map[string]int64{}, emitRaw: true}
	for seq := int64(1); seq <= 3; seq++ {
		if err := p.handle(reading("dev-0001", seq, t0)); err != nil {
			t.Fatal(err)
		}
	}

	res := make(chan error, 1)
	go func() { res <- p.flush() }()
	waitFor(t, "ожидание повтора сброса", func() bool { return clk.Waiters() >= 1 })

	if idx := j.index("checkpoint"); idx != -1 {
		t.Fatalf("чекпоинт записан при неуспешном Flush стока nats: журнал %v", j.list())
	}
	clk.Advance(flushRetryDelay)
	if err := <-res; err != nil {
		t.Fatalf("сброс после повтора: %v", err)
	}

	if got, _ := cps.Load(context.Background(), "dev-0001"); got != 3 {
		t.Fatalf("чекпоинт %d, ожидался 3", got)
	}
	if calls, _ := good.rawCount(); calls != 1 {
		t.Fatalf("files записан %d раз: удавшийся сток не должен писаться повторно", calls)
	}
	if calls, _ := bad.rawCount(); calls != 1 {
		t.Fatalf("nats: WriteRaw %d раз, ожидался 1 (повторяется только Flush)", calls)
	}
	if j.index("checkpoint") < j.index("flush:nats") {
		t.Fatalf("чекпоинт раньше Flush: %v", j.list())
	}
}

// Порядок graceful shutdown: опрос прекращён, канал дочитан, окна закрыты,
// буфер сброшен в стоки, чекпоинты записаны, lease отозван, стоки закрыты.
func TestGracefulShutdownOrder(t *testing.T) {
	cfg := baseConfig()
	cfg.Emit = "both"
	cfg.BatchSize = 1000
	j := &journal{}
	s := &recSink{name: "files", events: j}
	cps := &cpStore{MemoryStore: NewMemoryStore(), j: j}
	src := &stubSrc{readings: map[string][]model.Reading{
		"dev-0001": {reading("dev-0001", 1, t0), reading("dev-0001", 2, t0), reading("dev-0001", 3, t0)},
	}}
	clk := clock.NewFake(t0)
	c, err := New(Deps{Config: cfg, Log: quietLog(), Clock: clk, Source: src,
		Assigner: &stubAsg{devs: []string{"dev-0001"}, j: j}, Checkpoints: cps,
		Sinks: []sinks.Named{{Name: "files", Sink: s}}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- c.Run(ctx) }()

	waitFor(t, "показания получены", func() bool { return c.m.readings.Load() == 3 })
	cancel()
	if err := <-runErr; err != nil {
		t.Fatalf("Run: %v", err)
	}

	ev := j.list()
	// окна не закрыты по времени: закрытие выполняется при остановке
	writes := j.index("write:files")
	cp := j.index("checkpoint")
	asg := j.index("asgclose")
	closeIdx := j.index("close:files")
	if writes < 0 || cp < 0 || asg < 0 || closeIdx < 0 {
		t.Fatalf("не хватает событий: %v", ev)
	}
	if !(writes < cp && cp < asg && asg < closeIdx) {
		t.Fatalf("порядок остановки нарушен: %v", ev)
	}
	if got, _ := cps.Load(context.Background(), "dev-0001"); got != 3 {
		t.Fatalf("чекпоинт %d, ожидался 3", got)
	}
	if !s.closed {
		t.Fatal("сток не закрыт")
	}
	if c.agg.Stats().Open != 0 {
		t.Fatalf("после остановки открыто окон: %d", c.agg.Stats().Open)
	}
	if calls := j.index("writeagg:files"); calls < 0 {
		t.Fatalf("окна не записаны при остановке: %v", ev)
	}
}
