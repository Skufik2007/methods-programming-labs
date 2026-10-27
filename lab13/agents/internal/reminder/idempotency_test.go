package reminder

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

// Находка №4: оркестратор после таймаута публикует новую попытку, а старая задача
// остаётся в JetStream и тоже обрабатывается. Каждая обработка создавала напоминания
// со свежими UUID — пациент получал SMS дважды. Повторная обработка той же записи
// не должна добавлять напоминаний.
func TestRepeatedTaskDoesNotDuplicateReminders(t *testing.T) {
	now := time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)
	st := store.NewMemory()
	var params yaml.Node
	if err := yaml.Unmarshal([]byte("offsets: [24h, 2h]\nchannel: sms\n"), &params); err != nil {
		t.Fatal(err)
	}
	inst, err := New(core.Deps{
		AgentID: "reminder-1", Step: "reminder", Store: st, Loc: time.UTC,
		Clock: func() time.Time { return now }, Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, *params.Content[0])
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(map[string]any{
		"case": map[string]any{"patient_id": "p-1", "patient_name": "Иванова", "phone": "+7"},
		"previous": map[string]any{
			"appointment_id": "appt-1", "starts_at": "2026-10-15T09:00:00Z", "ends_at": "2026-10-15T09:30:00Z",
			"doctor_name": "Петров", "specialty": "therapist",
		},
	})
	for attempt := 1; attempt <= 2; attempt++ {
		task := core.Task{TaskID: "t-" + string(rune('0'+attempt)), RunID: "run-1", Step: "reminder",
			Kind: "reminder", Attempt: attempt, Payload: payload}
		if _, err := inst.Tasks.Handle(context.Background(), task); err != nil {
			t.Fatalf("попытка %d: %v", attempt, err)
		}
	}
	due, err := st.ZRangeByScore(context.Background(), DueKey, 0, 1e12, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 2 {
		t.Fatalf("после двух обработок одной записи ожидалось 2 напоминания (24h и 2h), получено %d", len(due))
	}
}
