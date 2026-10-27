package autoscale

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/bus"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/testutil"
)

// Наблюдатель и журнал поверх встроенного NATS: проверяем, что метрика очереди
// (num_pending + num_ack_pending) совпадает с состоянием JetStream.
func TestNATSBacklogAndEmit(t *testing.T) {
	url := testutil.StartNATS(t)
	ctx := context.Background()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	clock := core.Clock(func() time.Time { return time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC) })

	n, err := ConnectNATS(ctx, url, clock, log)
	if err != nil {
		t.Fatalf("ConnectNATS: %v", err)
	}
	defer n.Close()

	// Поток и консьюмер ещё не созданы.
	if _, found, err := n.Backlog(ctx, "triage"); err != nil || found {
		t.Fatalf("до создания потока: found=%v err=%v", found, err)
	}

	// Создаём поток и консьюмер так же, как агент.
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	b, err := bus.Connect(ctx, url, bus.Info{AgentID: "triage-1", Step: "triage"}, clock, log, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	if _, err := b.EnsureConsumer(ctx, "triage"); err != nil {
		t.Fatal(err)
	}

	// Потока уже есть, а консьюмер «step-triage» создан — found=true, очередь пуста.
	if backlog, found, err := n.Backlog(ctx, "triage"); err != nil || !found || backlog != 0 {
		t.Fatalf("пустая очередь: backlog=%d found=%v err=%v", backlog, found, err)
	}

	for i := 0; i < 3; i++ {
		if _, err := js.Publish(ctx, "tasks.triage", []byte(`{}`)); err != nil {
			t.Fatal(err)
		}
	}
	if backlog, _, err := n.Backlog(ctx, "triage"); err != nil || backlog != 3 {
		t.Fatalf("после 3 публикаций: backlog=%d err=%v", backlog, err)
	}

	// Одна задача в работе (выбрана, но не подтверждена) — входит в метрику.
	cons, err := js.Consumer(ctx, bus.StreamName, "step-triage")
	if err != nil {
		t.Fatal(err)
	}
	batch, err := cons.Fetch(1, jetstream.FetchMaxWait(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	for m := range batch.Messages() {
		_ = m // намеренно не подтверждаем
	}
	if backlog, _, err := n.Backlog(ctx, "triage"); err != nil || backlog != 3 {
		t.Fatalf("pending + ack_pending: backlog=%d err=%v, ожидалось 3", backlog, err)
	}

	// Другой шаг без консьюмера.
	if _, found, err := n.Backlog(ctx, "reminder"); err != nil || found {
		t.Fatalf("шаг без консьюмера: found=%v err=%v", found, err)
	}

	// События уходят в events.autoscaler с trace-полями из контекста.
	evCh := make(chan []byte, 1)
	sub, err := nc.Subscribe("events.autoscaler", func(m *nats.Msg) {
		select {
		case evCh <- m.Data:
		default:
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Unsubscribe()
	// Подписка должна дойти до сервера раньше публикации из другого соединения.
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	n.Emit(ctx, core.Event{Action: "scaled_up", Step: "triage", Summary: "тест"})
	select {
	case data := <-evCh:
		var ev core.Event
		if err := json.Unmarshal(data, &ev); err != nil {
			t.Fatal(err)
		}
		if ev.Action != "scaled_up" || ev.Source != "autoscaler" || ev.TS == "" {
			t.Fatalf("событие: %+v", ev)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("событие не пришло")
	}
}

func TestConnectNATSFailsOnBadURL(t *testing.T) {
	t.Parallel()
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	// Адрес без слушателя: сервис должен сдаться по контексту, а не зависнуть.
	if _, err := ConnectNATS(ctx, "nats://127.0.0.1:1", core.Clock(time.Now), slog.New(slog.NewTextHandler(io.Discard, nil))); err == nil {
		t.Fatal("ожидалась ошибка подключения")
	}
}
