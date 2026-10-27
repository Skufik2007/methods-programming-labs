package bus_test

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/bus"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/stats"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/testutil"
)

// scriptedHandler возвращает заранее заданный результат.
type scriptedHandler struct {
	out any
	err error
}

func (h scriptedHandler) Handle(context.Context, core.Task) (any, error) { return h.out, h.err }

// panicHandler паникует — агент должен превратить это в повторяемую ошибку, а не упасть.
type panicHandler struct{}

func (panicHandler) Handle(context.Context, core.Task) (any, error) {
	panic("сбой внутри обработчика")
}

// runOne отправляет одну задачу, ждёт результат и возвращает его.
func runOne(t *testing.T, h core.TaskHandler, ch bus.Chaos, payload any) core.Result {
	t.Helper()
	url := testutil.StartNATS(t)
	nc, js := newJS(t, url)
	results, err := nc.SubscribeSync("results.run-p")
	if err != nil {
		t.Fatal(err)
	}
	b := newAgentBus(t, url, bus.Info{AgentID: "triage-p", Step: "triage", Kind: "triage"},
		stats.New(store.NewMemory(), "triage", "triage-p", discardLog))
	runTasks(t, b, "triage", h, 1, ch)
	publishTask(t, js, taskMsg(t, "run-p", "task-p", payload))
	var r core.Result
	if err := json.Unmarshal(nextMsg(t, results, 10*time.Second).Data, &r); err != nil {
		t.Fatal(err)
	}
	return r
}

func TestPermanentHandlerErrorIsNotRetryable(t *testing.T) {
	t.Parallel()
	r := runOne(t, scriptedHandler{err: core.Permanent("жалоба пустая")}, bus.Chaos{}, triagePayload("x"))
	if r.Success || r.Retryable || r.Error != "жалоба пустая" {
		t.Fatalf("результат: %+v", r)
	}
}

func TestTransientHandlerErrorIsRetryable(t *testing.T) {
	t.Parallel()
	r := runOne(t, scriptedHandler{err: core.Transient("redis недоступен")}, bus.Chaos{}, triagePayload("x"))
	if r.Success || !r.Retryable {
		t.Fatalf("результат: %+v", r)
	}
}

func TestUnknownErrorIsTreatedAsRetryable(t *testing.T) {
	t.Parallel()
	r := runOne(t, scriptedHandler{err: errors.New("что-то непонятное")}, bus.Chaos{}, triagePayload("x"))
	if r.Success || !r.Retryable {
		t.Fatalf("неизвестная ошибка — повторяемая: %+v", r)
	}
}

func TestHandlerPanicBecomesRetryableFailure(t *testing.T) {
	t.Parallel()
	r := runOne(t, panicHandler{}, bus.Chaos{}, triagePayload("x"))
	if r.Success || !r.Retryable {
		t.Fatalf("паника должна стать повторяемой ошибкой: %+v", r)
	}
}

// Сбой по доле AGENT_FAIL_RATE=1: задача всегда завершается ошибкой и помечается повторяемой.
func TestChaosFailRateProducesRetryableFailure(t *testing.T) {
	t.Parallel()
	r := runOne(t, scriptedHandler{out: map[string]any{"urgency": "routine"}}, bus.Chaos{FailRate: 1},
		triagePayload("x"))
	if r.Success || !r.Retryable || r.Error != "simulated failure" {
		t.Fatalf("chaos: %+v", r)
	}
	out, _ := r.Output.(map[string]any)
	if len(out) != 0 {
		t.Errorf("при ошибке output пустой: %v", out)
	}
}

func TestConnectHonoursCancelledContext(t *testing.T) {
	t.Parallel()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	// Адрес без слушателя: повторы прерываются по контексту, а не ждут 10 попыток.
	started := time.Now()
	_, err := bus.Connect(ctx, "nats://127.0.0.1:1", bus.Info{AgentID: "x"}, clock(), discardLog, nil)
	if err == nil {
		t.Fatal("ожидалась ошибка подключения")
	}
	if time.Since(started) > 5*time.Second {
		t.Fatalf("отмена контекста не прервала повторы: %v", time.Since(started))
	}
}

func TestSubscribeDeliversAndRespondsOnlyWhenReplyPresent(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	nc, _ := newJS(t, url)
	counters := stats.New(store.NewMemory(), "booking", "booking-s", discardLog)
	b := newAgentBus(t, url, bus.Info{AgentID: "booking-s", Step: "booking", Kind: "booking"}, counters)

	calls := make(chan []byte, 2)
	_, err := b.Subscribe(core.Subscription{
		Subject: "test.echo", Span: "test.echo",
		Handle: func(_ context.Context, data []byte) ([]byte, bool) {
			calls <- data
			if string(data) == "fail" {
				return []byte(`{"ok":false}`), true
			}
			return []byte(`{"ok":true}`), false
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	// Подписка агента могла ещё не дойти до сервера: повторяем запрос до ответа.
	var m *nats.Msg
	deadline := time.Now().Add(5 * time.Second)
	for {
		m, err = nc.Request("test.echo", []byte("ok"), time.Second)
		if err == nil || time.Now().After(deadline) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err != nil || string(m.Data) != `{"ok":true}` {
		t.Fatalf("ответ на request: %v", err)
	}
	// сбой учитывается в счётчике
	if _, err := nc.Request("test.echo", []byte("fail"), 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if counters.Failed() != 1 || counters.Processed() != 2 {
		t.Fatalf("счётчики: processed=%d failed=%d", counters.Processed(), counters.Failed())
	}
	// публикация без reply — обработчик вызывается, ответа нет
	if err := nc.Publish("test.echo", []byte("silent")); err != nil {
		t.Fatal(err)
	}
	select {
	case <-calls:
	case <-time.After(5 * time.Second):
		t.Fatal("обработчик не вызван")
	}
}

func TestPublishAndEmitCarryContext(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	nc, _ := newJS(t, url)
	sub, err := nc.SubscribeSync("custom.subject")
	if err == nil {
		err = nc.Flush()
	}
	if err != nil {
		t.Fatal(err)
	}
	b := newAgentBus(t, url, bus.Info{AgentID: "triage-e", Step: "triage"},
		stats.New(store.NewMemory(), "triage", "triage-e", discardLog))
	if err := b.Publish(context.Background(), "custom.subject", map[string]any{"k": "v"}); err != nil {
		t.Fatal(err)
	}
	m := nextMsg(t, sub, 5*time.Second)
	if string(m.Data) != `{"k":"v"}` {
		t.Fatalf("payload: %s", m.Data)
	}

	// Publish с несериализуемым значением — ошибка, а не паника.
	if err := b.Publish(context.Background(), "custom.subject", make(chan int)); err == nil {
		t.Fatal("ожидалась ошибка сериализации")
	}

	// Emit заполняет ts, source, step и отправляет в events.<source>.
	ev, err := nc.SubscribeSync("events.triage-e")
	if err == nil {
		err = nc.Flush()
	}
	if err != nil {
		t.Fatal(err)
	}
	b.Emit(context.Background(), core.Event{Action: "received", RunID: "r"})
	var got core.Event
	if err := json.Unmarshal(nextMsg(t, ev, 5*time.Second).Data, &got); err != nil {
		t.Fatal(err)
	}
	if got.Source != "triage-e" || got.Step != "triage" || got.TS == "" || got.Action != "received" {
		t.Fatalf("событие: %+v", got)
	}
}
