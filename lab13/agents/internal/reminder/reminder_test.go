package reminder

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

func TestParseOffset(t *testing.T) {
	t.Parallel()
	cases := []struct {
		in      string
		want    time.Duration
		wantErr bool
	}{
		{"24h", 24 * time.Hour, false},
		{"2h", 2 * time.Hour, false},
		{"90m", 90 * time.Minute, false},
		{"1d", 24 * time.Hour, false},
		{"1.5d", 36 * time.Hour, false},
		{"  2h  ", 2 * time.Hour, false},
		{"0h", 0, true},
		{"-1h", 0, true},
		{"0d", 0, true},
		{"-1d", 0, true},
		{"d", 0, true},
		{"", 0, true},
		{"сутки", 0, true},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			t.Parallel()
			got, err := ParseOffset(c.in)
			if (err != nil) != c.wantErr {
				t.Fatalf("err = %v, ожидалось ошибки: %v", err, c.wantErr)
			}
			if !c.wantErr && got != c.want {
				t.Fatalf("ParseOffset = %v, ожидалось %v", got, c.want)
			}
		})
	}
}

var (
	startsAt = time.Date(2026, 10, 15, 9, 0, 0, 0, time.UTC)  // приём
	offsets  = []time.Duration{2 * time.Hour, 24 * time.Hour} // намеренно не по порядку
)

func TestScheduleAllFutureOffsets(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)
	plans := Schedule(startsAt, now, offsets, 0)
	if len(plans) != 2 {
		t.Fatalf("напоминаний %d, ожидалось 2", len(plans))
	}
	// раньше всего — за 24 часа
	if want := startsAt.Add(-24 * time.Hour); !plans[0].SendAt.Equal(want) {
		t.Errorf("первое напоминание %v, ожидалось %v", plans[0].SendAt, want)
	}
	if want := startsAt.Add(-2 * time.Hour); !plans[1].SendAt.Equal(want) {
		t.Errorf("второе напоминание %v, ожидалось %v", plans[1].SendAt, want)
	}
	if plans[0].ID == plans[1].ID || plans[0].ID == "" {
		t.Errorf("идентификаторы должны быть уникальными и непустыми")
	}
}

func TestSchedulePastOffsetsSkipped(t *testing.T) {
	t.Parallel()
	// 14.10 10:00 UTC: за 24 часа (14.10 09:00) уже поздно, за 2 часа — ещё нет.
	now := time.Date(2026, 10, 14, 10, 0, 0, 0, time.UTC)
	plans := Schedule(startsAt, now, offsets, 0)
	if len(plans) != 1 {
		t.Fatalf("напоминаний %d, ожидалось 1 (прошедшее 24h пропущено)", len(plans))
	}
	if want := startsAt.Add(-2 * time.Hour); !plans[0].SendAt.Equal(want) {
		t.Errorf("SendAt = %v, ожидалось %v", plans[0].SendAt, want)
	}
}

func TestScheduleAllPastGivesSingleNow(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 15, 8, 0, 0, 0, time.UTC) // до приёма 1 час — все смещения прошли
	plans := Schedule(startsAt, now, offsets, 0)
	if len(plans) != 1 {
		t.Fatalf("напоминаний %d, ожидалось одно «сейчас»", len(plans))
	}
	if !plans[0].SendAt.Equal(now) {
		t.Fatalf("SendAt = %v, ожидалось now %v", plans[0].SendAt, now)
	}
}

func TestScheduleDemoDelay(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)
	demo := 20 * time.Second
	plans := Schedule(startsAt, now, offsets, demo)
	if len(plans) != 2 {
		t.Fatalf("напоминаний %d", len(plans))
	}
	if !plans[0].SendAt.Equal(now.Add(demo)) || !plans[1].SendAt.Equal(now.Add(2*demo)) {
		t.Fatalf("демо: %v и %v, ожидалось now+20s и now+40s", plans[0].SendAt, plans[1].SendAt)
	}
	// демо-задержка с одним «сейчас»
	single := Schedule(startsAt, startsAt.Add(time.Hour), offsets, demo)
	if len(single) != 1 || !single[0].SendAt.Equal(startsAt.Add(time.Hour).Add(demo)) {
		t.Fatalf("демо для одного напоминания: %+v", single)
	}
}

func TestScheduleSendAtIsUTC(t *testing.T) {
	t.Parallel()
	loc := time.FixedZone("MSK", 3*3600)
	now := time.Date(2026, 10, 13, 10, 0, 0, 0, loc)
	plans := Schedule(startsAt.In(loc), now, []time.Duration{time.Hour}, 0)
	if len(plans) != 1 || plans[0].SendAt.Location() != time.UTC {
		t.Fatalf("SendAt должен быть в UTC: %+v", plans)
	}
}

func newHandler(t *testing.T, yamlSrc string, st store.Store, clockAt time.Time) (core.Instance, error) {
	t.Helper()
	var params yaml.Node
	if err := yaml.Unmarshal([]byte(yamlSrc), &params); err != nil {
		t.Fatal(err)
	}
	loc, _ := time.LoadLocation("Europe/Moscow")
	return New(core.Deps{
		AgentID: "reminder-test", Step: "reminder", Store: st, Loc: loc,
		Clock: func() time.Time { return clockAt },
		Log:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, *params.Content[0])
}

func reminderPayload(t *testing.T, apptID string) json.RawMessage {
	t.Helper()
	p, err := json.Marshal(map[string]any{
		"case": map[string]any{"patient_id": "p-9", "patient_name": "Петров", "phone": "+7 900"},
		"previous": map[string]any{
			"appointment_id": apptID, "starts_at": "2026-10-15T09:00:00Z", "ends_at": "2026-10-15T09:20:00Z",
			"doctor_name": "Смирнова", "specialty": "therapist",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestHandleSchedulesAndDescribes(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	inst, err := newHandler(t, "offsets: [24h, 2h]\n", st, time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	res, err := inst.Tasks.Handle(context.Background(), core.Task{TaskID: "t", RunID: "run-7",
		Kind: "reminder", Payload: reminderPayload(t, "appt-7")})
	if err != nil {
		t.Fatalf("Handle: %v", err)
	}
	out := res.(map[string]any)
	list := out["reminders"].([]map[string]any)
	if len(list) != 2 {
		t.Fatalf("напоминаний %d", len(list))
	}
	if out["appointment_id"] != "appt-7" || out["starts_at"] != "2026-10-15T09:00:00Z" || out["ends_at"] != "2026-10-15T09:20:00Z" {
		t.Errorf("служебные поля вывода: %v", out)
	}
	for _, r := range list {
		if r["channel"] != "sms" || r["status"] != "scheduled" {
			t.Errorf("элемент вывода: %v", r)
		}
	}

	// Текст и поля в хранилище.
	id := list[0]["reminder_id"].(string)
	rec, err := st.HGetAll(context.Background(), ItemPrefix+id)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(rec["text"], "Смирнова") || !strings.Contains(rec["text"], "Терапевт") {
		t.Errorf("текст напоминания: %q", rec["text"])
	}
	// Время по местному времени клиники: 09:00 UTC = 12:00 MSK.
	if !strings.Contains(rec["text"], "15.10.2026 12:00 MSK") {
		t.Errorf("время по Москве в тексте: %q", rec["text"])
	}
	if rec["run_id"] != "run-7" || rec["patient_name"] != "Петров" || rec["appointment_id"] != "appt-7" {
		t.Errorf("поля записи: %v", rec)
	}
}

// Повторная обработка той же записи не создаёт дублей (см. idempotency_test.go). Здесь —
// что идентификаторы напоминаний детерминированы по appointment_id.
func TestHandleStableIDsForSameAppointment(t *testing.T) {
	t.Parallel()
	ids := func() []string {
		st := store.NewMemory()
		inst, err := newHandler(t, "offsets: [24h, 2h]\n", st, time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC))
		if err != nil {
			t.Fatal(err)
		}
		res, err := inst.Tasks.Handle(context.Background(), core.Task{RunID: "r", Payload: reminderPayload(t, "appt-x")})
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, r := range res.(map[string]any)["reminders"].([]map[string]any) {
			out = append(out, r["reminder_id"].(string))
		}
		return out
	}
	a, b := ids(), ids()
	if len(a) != 2 || a[0] != b[0] || a[1] != b[1] {
		t.Fatalf("идентификаторы не детерминированы: %v и %v", a, b)
	}
}

func TestHandleWithoutAppointmentUsesRandomIDs(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	inst, err := newHandler(t, "offsets: [24h]\n", st, time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	payload := reminderPayload(t, "")
	res1, err := inst.Tasks.Handle(context.Background(), core.Task{RunID: "r", Payload: payload})
	if err != nil {
		t.Fatal(err)
	}
	res2, err := inst.Tasks.Handle(context.Background(), core.Task{RunID: "r", Payload: payload})
	if err != nil {
		t.Fatal(err)
	}
	a := res1.(map[string]any)["reminders"].([]map[string]any)[0]["reminder_id"]
	b := res2.(map[string]any)["reminders"].([]map[string]any)[0]["reminder_id"]
	if a == b {
		t.Fatal("без appointment_id каждое планирование даёт новый id")
	}
}

func TestHandleErrors(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)

	inst, err := newHandler(t, "offsets: [24h]\n", store.NewMemory(), now)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	if _, err := inst.Tasks.Handle(ctx, core.Task{Payload: json.RawMessage(`{"case":`)}); err == nil || core.IsRetryable(err) {
		t.Errorf("битый JSON должен быть постоянной ошибкой: %v", err)
	}
	noStart, _ := json.Marshal(map[string]any{"previous": map[string]any{"appointment_id": "a"}})
	if _, err := inst.Tasks.Handle(ctx, core.Task{Payload: noStart}); err == nil || core.IsRetryable(err) ||
		!strings.Contains(err.Error(), "starts_at") {
		t.Errorf("нет starts_at: %v", err)
	}

	// Сбой Redis при постановке — повторяемая ошибка.
	fs := &failSetNX{Store: store.NewMemory()}
	inst2, err := newHandler(t, "offsets: [24h]\n", fs, now)
	if err != nil {
		t.Fatal(err)
	}
	_, err = inst2.Tasks.Handle(ctx, core.Task{RunID: "r", Payload: reminderPayload(t, "a")})
	if err == nil || !core.IsRetryable(err) {
		t.Fatalf("сбой Redis должен быть повторяемым: %v", err)
	}
}

type failSetNX struct{ store.Store }

func (f *failSetNX) SetNX(context.Context, string, string) (bool, error) {
	return false, errors.New("сеть")
}

func TestNewValidation(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		yaml string
	}{
		{"пустые смещения", "offsets: []\n"},
		{"некорректное смещение", "offsets: [вчера]\n"},
		{"неверный тип", "offsets: 24h\n"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if _, err := newHandler(t, c.yaml, store.NewMemory(), time.Now()); err == nil {
				t.Fatal("ожидалась ошибка параметров")
			}
		})
	}
}

func TestNewDefaultChannelAndBackground(t *testing.T) {
	t.Parallel()
	inst, err := newHandler(t, "offsets: [24h]\n", store.NewMemory(), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(inst.Background) != 1 {
		t.Fatalf("фоновых циклов %d, ожидался 1 (диспетчер)", len(inst.Background))
	}
	inst2, err := newHandler(t, "offsets: [24h]\nchannel: email\n", store.NewMemory(), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	res, _ := inst2.Tasks.Handle(context.Background(), core.Task{RunID: "r", Payload: reminderPayload(t, "a")})
	if res.(map[string]any)["reminders"].([]map[string]any)[0]["channel"] != "email" {
		t.Error("канал из параметров не применён")
	}
	inst3, _ := newHandler(t, "offsets: [24h]\n", store.NewMemory(), time.Now())
	res, _ = inst3.Tasks.Handle(context.Background(), core.Task{RunID: "r", Payload: reminderPayload(t, "b")})
	if res.(map[string]any)["reminders"].([]map[string]any)[0]["channel"] != "sms" {
		t.Error("канал по умолчанию — sms")
	}
}
