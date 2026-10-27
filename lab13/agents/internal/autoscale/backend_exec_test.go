package autoscale

import (
	"context"
	"io"
	"log/slog"
	"os"
	"testing"
	"time"
)

// Процесс-заглушка агента: тестовый бинарник сам запускается с переменной окружения и
// либо висит (sleep), либо сразу завершается (exit). Аргументы -role игнорируются.
func TestMain(m *testing.M) {
	switch os.Getenv("AUTOSCALE_TEST_HELPER") {
	case "sleep":
		time.Sleep(10 * time.Minute)
		os.Exit(0)
	case "exit":
		os.Exit(3)
	}
	os.Exit(m.Run())
}

// Тесты используют t.Setenv, поэтому не выполняются параллельно.
func execBackend(t *testing.T, mode string) *ExecBackend {
	t.Helper()
	t.Setenv("AUTOSCALE_TEST_HELPER", mode)
	bin, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return NewExecBackend(ExecConfig{AgentBin: bin, StopTimeout: 2 * time.Second},
		slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func TestExecStartListStop(t *testing.T) {
	b := execBackend(t, "sleep")
	ctx := context.Background()
	sc := StepConfig{Step: "triage", Role: "roles/triage.md"}

	first, err := b.Start(ctx, sc)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	second, err := b.Start(ctx, sc)
	if err != nil {
		t.Fatalf("Start #2: %v", err)
	}
	if first.ID != "triage-auto-1" || second.ID != "triage-auto-2" || second.Seq != 2 {
		t.Fatalf("номера экземпляров: %+v %+v", first, second)
	}

	list, err := b.List(ctx, "triage")
	if err != nil || len(list) != 2 || list[0].ID != first.ID {
		t.Fatalf("List: %+v %v", list, err)
	}
	if other, _ := b.List(ctx, "booking"); len(other) != 0 {
		t.Fatalf("экземпляры другого шага: %v", other)
	}

	if err := b.Stop(ctx, first); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	list, _ = b.List(ctx, "triage")
	if len(list) != 1 || list[0].ID != second.ID {
		t.Fatalf("после остановки: %+v", list)
	}
	// повторная остановка уже остановленного — не ошибка
	if err := b.Stop(ctx, first); err != nil {
		t.Fatalf("повторный Stop: %v", err)
	}
	if err := b.Stop(ctx, second); err != nil {
		t.Fatalf("Stop второго: %v", err)
	}
}

func TestExecStopCancelledContextKills(t *testing.T) {
	b := execBackend(t, "sleep")
	inst, err := b.Start(context.Background(), StepConfig{Step: "triage", Role: "r"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // отмена сразу: процесс убивается без ожидания stopTimeout
	started := time.Now()
	if err := b.Stop(ctx, inst); err != nil {
		t.Fatalf("Stop с отменённым контекстом: %v", err)
	}
	if time.Since(started) > 5*time.Second {
		t.Fatalf("остановка заняла слишком долго: %v", time.Since(started))
	}
}

func TestExecCrashedProcessIsForgotten(t *testing.T) {
	b := execBackend(t, "exit")
	ctx := context.Background()
	if _, err := b.Start(ctx, StepConfig{Step: "triage", Role: "r"}); err != nil {
		t.Fatal(err)
	}
	// Завершившийся процесс пропадает из списка — политика запустит новый.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		list, _ := b.List(ctx, "triage")
		if len(list) == 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("завершившийся процесс не удалён из списка")
}

func TestExecStartFailsForMissingBinary(t *testing.T) {
	b := NewExecBackend(ExecConfig{AgentBin: "определённо-нет-такого-бинарника", StopTimeout: time.Second},
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	if _, err := b.Start(context.Background(), StepConfig{Step: "triage", Role: "r"}); err == nil {
		t.Fatal("ожидалась ошибка запуска")
	}
	if list, _ := b.List(context.Background(), "triage"); len(list) != 0 {
		t.Fatal("неудачный запуск не должен попасть в список")
	}
}

func TestExecStopUnknownInstance(t *testing.T) {
	b := NewExecBackend(ExecConfig{AgentBin: "x", StopTimeout: time.Second},
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := b.Stop(context.Background(), Instance{ID: "triage-auto-9"}); err != nil {
		t.Fatalf("неизвестный экземпляр — уже остановлен: %v", err)
	}
}
