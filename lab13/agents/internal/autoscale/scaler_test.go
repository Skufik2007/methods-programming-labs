package autoscale

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
)

// fakeObs — наблюдатель очереди: длина по шагу, found=false — консьюмера нет.
type fakeObs struct {
	mu      sync.Mutex
	backlog map[string]int
	missing map[string]bool
	err     error
}

func (f *fakeObs) Backlog(_ context.Context, step string) (int, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return 0, false, f.err
	}
	if f.missing[step] {
		return 0, false, nil
	}
	return f.backlog[step], true, nil
}

func (f *fakeObs) set(step string, n int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.backlog[step] = n
}

// fakeBackend — бэкенд в памяти; startErr/stopErr имитируют сбой Docker или процесса.
type fakeBackend struct {
	mu       sync.Mutex
	insts    map[string][]Instance
	seq      int
	startErr error
	stopErr  error
	stopped  []string
}

func newFakeBackend() *fakeBackend { return &fakeBackend{insts: map[string][]Instance{}} }

func (b *fakeBackend) List(_ context.Context, step string) ([]Instance, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := append([]Instance(nil), b.insts[step]...)
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, nil
}

func (b *fakeBackend) Start(_ context.Context, sc StepConfig) (Instance, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.startErr != nil {
		return Instance{}, b.startErr
	}
	b.seq++
	inst := Instance{ID: instanceID(sc.Step, b.seq), Step: sc.Step, Seq: b.seq}
	b.insts[sc.Step] = append(b.insts[sc.Step], inst)
	return inst, nil
}

func (b *fakeBackend) Stop(_ context.Context, inst Instance) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.stopErr != nil {
		return b.stopErr
	}
	list := b.insts[inst.Step]
	for i, x := range list {
		if x.ID == inst.ID {
			b.insts[inst.Step] = append(list[:i:i], list[i+1:]...)
			b.stopped = append(b.stopped, inst.ID)
			return nil
		}
	}
	return nil
}

func (b *fakeBackend) count(step string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.insts[step])
}

// fakeEmit — журнал событий в памяти.
type fakeEmit struct {
	mu     sync.Mutex
	events []core.Event
}

func (e *fakeEmit) Emit(_ context.Context, ev core.Event) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.events = append(e.events, ev)
}

func (e *fakeEmit) actions() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	var out []string
	for _, ev := range e.events {
		out = append(out, ev.Action)
	}
	return out
}

// fakeClock — управляемое «сейчас».
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

func (c *fakeClock) core() core.Clock { return func() time.Time { return c.Now() } }

var discardLog = slog.New(slog.NewTextHandler(io.Discard, nil))

func triageStep() StepConfig {
	return StepConfig{Step: "triage", Role: "roles/triage.md", PerInstance: 5, Min: 1, Max: 3,
		Cooldown: 30 * time.Second}
}

type harness struct {
	s     *Scaler
	obs   *fakeObs
	be    *fakeBackend
	em    *fakeEmit
	clock *fakeClock
}

func newHarness(steps ...StepConfig) *harness {
	h := &harness{
		obs:   &fakeObs{backlog: map[string]int{}, missing: map[string]bool{}},
		be:    newFakeBackend(),
		em:    &fakeEmit{},
		clock: &fakeClock{now: time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)},
	}
	h.s = New(steps, time.Second, h.obs, h.be, h.em, h.clock.core(), discardLog)
	return h
}

func TestScaleUpAndDown(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())

	// Очередь 11 задач при 5 на экземпляр: нужно 3 — добавляем 2 экземпляра.
	h.obs.set("triage", 11)
	h.s.Reconcile(context.Background())
	if n := h.be.count("triage"); n != 2 {
		t.Fatalf("добавочных экземпляров %d, ожидалось 2", n)
	}
	if got := h.em.actions(); len(got) != 1 || got[0] != "scaled_up" {
		t.Fatalf("события: %v", got)
	}
	st := h.s.Snapshot().Steps[0]
	if st.Total != 3 || st.Desired != 3 || st.Static != 1 || len(st.Auto) != 2 || !st.ConsumerFound {
		t.Fatalf("состояние: %+v", st)
	}

	// Очередь опустела, но cooldown ещё не прошёл — ничего не останавливаем.
	h.obs.set("triage", 0)
	h.clock.Set(h.clock.Now().Add(10 * time.Second))
	h.s.Reconcile(context.Background())
	if n := h.be.count("triage"); n != 2 {
		t.Fatalf("до истечения cooldown экземпляры не должны уходить: %d", n)
	}

	// После cooldown — останавливаем лишние, самые новые первыми.
	h.clock.Set(h.clock.Now().Add(25 * time.Second))
	h.s.Reconcile(context.Background())
	if n := h.be.count("triage"); n != 0 {
		t.Fatalf("после cooldown добавочных экземпляров %d, ожидалось 0", n)
	}
	if len(h.be.stopped) != 2 || h.be.stopped[0] != "triage-auto-2" || h.be.stopped[1] != "triage-auto-1" {
		t.Fatalf("порядок остановки: %v", h.be.stopped)
	}
	if got := strings.Join(h.em.actions(), ","); got != "scaled_up,scaled_down" {
		t.Fatalf("события: %s", got)
	}
}

func TestMaxIsRespected(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 1000)
	h.s.Reconcile(context.Background())
	if n := h.be.count("triage"); n != 2 { // max 3 общих, static 1 => 2 добавочных
		t.Fatalf("добавочных %d, ожидалось 2 (max 3 всего)", n)
	}
}

func TestNoConsumerMeansNoScaling(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.missing["triage"] = true
	h.obs.set("triage", 50)
	h.s.Reconcile(context.Background())
	if h.be.count("triage") != 0 || len(h.em.actions()) != 0 {
		t.Fatal("без консьюмера масштабировать нечего")
	}
	st := h.s.Snapshot().Steps[0]
	if st.ConsumerFound {
		t.Fatal("ConsumerFound должен быть false")
	}
}

func TestObserverErrorIsLoggedNotFatal(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.err = errors.New("сеть")
	h.s.Reconcile(context.Background()) // не должно паниковать
	if h.be.count("triage") != 0 {
		t.Fatal("при ошибке наблюдения экземпляры не трогаем")
	}
}

func TestBackendListErrorIsLoggedNotFatal(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 20)
	lb := &listErrBackend{fakeBackend: h.be}
	h.s = New([]StepConfig{triageStep()}, time.Second, h.obs, lb, h.em, h.clock.core(), discardLog)
	h.s.Reconcile(context.Background())
	if len(h.em.actions()) != 0 {
		t.Fatal("при ошибке списка событий быть не должно")
	}
}

type listErrBackend struct{ *fakeBackend }

func (l *listErrBackend) List(context.Context, string) ([]Instance, error) {
	return nil, errors.New("docker недоступен")
}

func TestStartFailureStopsScalingUp(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.be.startErr = errors.New("образ не найден")
	h.obs.set("triage", 15)
	h.s.Reconcile(context.Background())
	if h.be.count("triage") != 0 {
		t.Fatal("ни один экземпляр не запущен")
	}
	if len(h.em.actions()) != 0 {
		t.Fatalf("без запуска события scaled_up не нужны: %v", h.em.actions())
	}
}

func TestPartialStartStillAnnounced(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	// Первый запуск проходит, второй падает.
	h.obs.set("triage", 15)
	flaky := &flakyStartBackend{fakeBackend: h.be, failAfter: 1}
	h.s = New([]StepConfig{triageStep()}, time.Second, h.obs, flaky, h.em, h.clock.core(), discardLog)
	h.s.Reconcile(context.Background())
	if h.be.count("triage") != 1 {
		t.Fatalf("запущено %d, ожидался 1", h.be.count("triage"))
	}
	if got := strings.Join(h.em.actions(), ","); got != "scaled_up" {
		t.Fatalf("о частичном запуске объявляем: %s", got)
	}
}

type flakyStartBackend struct {
	*fakeBackend
	failAfter int
	calls     int
}

func (f *flakyStartBackend) Start(ctx context.Context, sc StepConfig) (Instance, error) {
	f.calls++
	if f.calls > f.failAfter {
		return Instance{}, errors.New("сбой")
	}
	return f.fakeBackend.Start(ctx, sc)
}

func TestStopFailureKeepsInstance(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 11)
	h.s.Reconcile(context.Background())
	h.be.stopErr = errors.New("не остановился")
	h.obs.set("triage", 0)
	h.clock.Set(h.clock.Now().Add(time.Minute))
	h.s.Reconcile(context.Background())
	if h.be.count("triage") != 2 {
		t.Fatalf("при ошибке остановки экземпляры остаются: %d", h.be.count("triage"))
	}
	for _, a := range h.em.actions() {
		if a == "scaled_down" {
			t.Fatal("scaled_down не должен публиковаться без остановки")
		}
	}
}

func TestSeveralStepsIndependent(t *testing.T) {
	t.Parallel()
	bookingStep := StepConfig{Step: "booking", Role: "roles/booking-a.md", PerInstance: 2, Min: 0, Max: 4,
		Cooldown: time.Second}
	h := newHarness(triageStep(), bookingStep)
	h.obs.set("triage", 11)
	h.obs.set("booking", 3)
	h.s.Reconcile(context.Background())
	if h.be.count("triage") != 2 || h.be.count("booking") != 2 {
		t.Fatalf("triage=%d booking=%d", h.be.count("triage"), h.be.count("booking"))
	}
}

func TestReconcileStopsOnCancelledContext(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 20)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	h.s.Reconcile(ctx)
	if h.be.count("triage") != 0 {
		t.Fatal("при отменённом контексте новых экземпляров быть не должно")
	}
}

func TestRunReconcilesUntilCancelled(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 6)
	h.s = New([]StepConfig{triageStep()}, 5*time.Millisecond, h.obs, h.be, h.em, h.clock.core(), discardLog)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { h.s.Run(ctx); close(done) }()

	deadline := time.Now().Add(2 * time.Second)
	for h.be.count("triage") == 0 && time.Now().Before(deadline) {
		time.Sleep(2 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run не остановился")
	}
	if h.be.count("triage") != 1 { // 6 задач при 5 на экземпляр: 2 всего, 1 добавочный
		t.Fatalf("добавочных %d, ожидался 1", h.be.count("triage"))
	}
}

func TestShutdownStopsAllExtras(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 16)
	h.s.Reconcile(context.Background())
	if h.be.count("triage") != 2 {
		t.Fatalf("до остановки экземпляров %d", h.be.count("triage"))
	}
	h.s.Shutdown(context.Background())
	if h.be.count("triage") != 0 {
		t.Fatalf("после Shutdown остались: %d", h.be.count("triage"))
	}
	events := strings.Join(h.em.actions(), ",")
	if !strings.HasSuffix(events, "scaled_down") {
		t.Fatalf("Shutdown должен объявить scaled_down: %s", events)
	}
}

func TestShutdownWithoutExtrasEmitsNothing(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.s.Shutdown(context.Background())
	if len(h.em.actions()) != 0 {
		t.Fatalf("без экземпляров событий быть не должно: %v", h.em.actions())
	}
}

func TestShutdownListError(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	lb := &listErrBackend{fakeBackend: h.be}
	h.s = New([]StepConfig{triageStep()}, time.Second, h.obs, lb, h.em, h.clock.core(), discardLog)
	h.s.Shutdown(context.Background()) // ошибка списка логируется, без паники
}

func TestShutdownStopErrorIsLogged(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 11)
	h.s.Reconcile(context.Background())
	h.be.stopErr = errors.New("не остановился")
	h.s.Shutdown(context.Background())
	if h.be.count("triage") != 2 {
		t.Fatal("ошибка остановки не должна удалять экземпляры из учёта")
	}
}

func TestSnapshotIsCopy(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 11)
	h.s.Reconcile(context.Background())
	snap := h.s.Snapshot()
	snap.Steps[0].Auto[0] = "мусор"
	if h.s.Snapshot().Steps[0].Auto[0] == "мусор" {
		t.Fatal("Snapshot должен возвращать копию")
	}
}

func TestServeStatusEndpoint(t *testing.T) {
	t.Parallel()
	h := newHarness(triageStep())
	h.obs.set("triage", 11)
	h.s.Reconcile(context.Background())

	port := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	errc := make(chan error, 1)
	go func() { errc <- h.s.ServeStatus(ctx, port) }()

	var resp *http.Response
	var err error
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		resp, err = http.Get("http://127.0.0.1:" + strconv.Itoa(port) + "/status")
		if err == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err != nil {
		t.Fatalf("GET /status: %v", err)
	}
	defer resp.Body.Close()
	if resp.Header.Get("Access-Control-Allow-Origin") != "*" {
		t.Error("нет CORS-заголовка для дашборда")
	}
	var st Status
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		t.Fatal(err)
	}
	if len(st.Steps) != 1 || st.Steps[0].Total != 3 {
		t.Fatalf("status: %+v", st)
	}

	cancel()
	select {
	case err := <-errc:
		if err != nil {
			t.Fatalf("ServeStatus после отмены: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("ServeStatus не остановился")
	}
}

func TestServeStatusPortBusy(t *testing.T) {
	t.Parallel()
	// Занимаем тот же адрес, который использует ServeStatus (все интерфейсы).
	ln, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	port := ln.Addr().(*net.TCPAddr).Port
	h := newHarness(triageStep())
	if err := h.s.ServeStatus(context.Background(), port); err == nil {
		t.Fatal("занятый порт должен давать ошибку")
	}
}

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}
