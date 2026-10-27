package stats

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

func silent() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestKey(t *testing.T) {
	t.Parallel()
	if Key("triage") != "mas:stats:triage" {
		t.Fatalf("Key = %q", Key("triage"))
	}
}

func TestCountersAggregateAndRestore(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	st := store.NewMemory()

	c := New(st, "triage", "triage-1", silent())
	c.Done(ctx, false)
	c.Done(ctx, true)
	if c.Processed() != 2 || c.Failed() != 1 {
		t.Fatalf("в памяти: processed=%d failed=%d", c.Processed(), c.Failed())
	}
	m, _ := st.HGetAll(ctx, Key("triage"))
	if m["processed"] != "2" || m["failed"] != "1" || m["processed:triage-1"] != "2" || m["failed:triage-1"] != "1" {
		t.Fatalf("счётчики в хранилище: %v", m)
	}

	// Рестарт экземпляра с тем же AGENT_ID подхватывает значения.
	again := New(st, "triage", "triage-1", silent())
	again.Load(ctx)
	if again.Processed() != 2 || again.Failed() != 1 {
		t.Fatalf("после рестарта: processed=%d failed=%d", again.Processed(), again.Failed())
	}
	// Другой экземпляр того же шага не наследует персональные поля.
	other := New(st, "triage", "triage-2", silent())
	other.Load(ctx)
	if other.Processed() != 0 {
		t.Fatalf("персональные счётчики чужие: %d", other.Processed())
	}
}

func TestStatsErrorsAreNotFatal(t *testing.T) {
	t.Parallel()
	c := New(&brokenStore{Store: store.NewMemory()}, "s", "a", silent())
	c.Load(context.Background())
	if c.Processed() != 0 {
		t.Fatal("после ошибки чтения счётчики остаются нулевыми")
	}
	c.Done(context.Background(), true) // ошибка записи не должна паниковать
	if c.Processed() != 1 || c.Failed() != 1 {
		t.Fatalf("локальные счётчики: %d %d", c.Processed(), c.Failed())
	}
}

type brokenStore struct{ store.Store }

func (b *brokenStore) HGetAll(context.Context, string) (map[string]string, error) {
	return nil, errors.New("сеть")
}

func (b *brokenStore) HIncrBy(context.Context, string, string, int64) (int64, error) {
	return 0, errors.New("сеть")
}
