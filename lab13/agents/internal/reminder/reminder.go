// Пакет reminder — планирование напоминаний о приёме. Сама отправка выполняется общим
// диспетчером (пакет dispatch) из Redis, поэтому переживает рестарт агента.
package reminder

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/directory"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/dispatch"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/stats"
)

const (
	DueKey     = "mas:reminders:due"
	ItemPrefix = "mas:reminder:"
)

type Params struct {
	Offsets []string `yaml:"offsets"` // «24h», «2h», «1d»
	Channel string   `yaml:"channel"`
}

// Plan — один запланированный пункт.
type Plan struct {
	ID     string
	SendAt time.Time
}

// Schedule вычисляет моменты отправки. Чистая функция: часы и demo передаются явно.
// Прошедшие смещения пропускаются; если не осталось ни одного — одно напоминание «сейчас».
// При demo > 0 i-е напоминание уходит через demo·(i+1) от now.
func Schedule(startsAt, now time.Time, offsets []time.Duration, demo time.Duration) []Plan {
	sorted := append([]time.Duration(nil), offsets...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] > sorted[j] }) // раньше всего — самое большое смещение
	var times []time.Time
	for _, off := range sorted {
		if at := startsAt.Add(-off); at.After(now) {
			times = append(times, at)
		}
	}
	if len(times) == 0 {
		times = []time.Time{now}
	}
	if demo > 0 {
		for i := range times {
			times[i] = now.Add(demo * time.Duration(i+1))
		}
	}
	plans := make([]Plan, len(times))
	for i, t := range times {
		plans[i] = Plan{ID: uuid.NewString(), SendAt: t.UTC()}
	}
	return plans
}

// ParseOffset понимает «24h», «90m», а также «1d» (сутки).
func ParseOffset(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if n, ok := strings.CutSuffix(s, "d"); ok {
		days, err := strconv.ParseFloat(n, 64)
		if err != nil || days <= 0 {
			return 0, fmt.Errorf("смещение %q: ожидается положительное число суток", s)
		}
		return time.Duration(days * float64(24*time.Hour)), nil
	}
	d, err := time.ParseDuration(s)
	if err != nil || d <= 0 {
		return 0, fmt.Errorf("смещение %q: ожидается длительность вида 24h", s)
	}
	return d, nil
}

type Handler struct {
	offsets []time.Duration
	channel string
	disp    *dispatch.Dispatcher
	clock   core.Clock
	loc     *time.Location
	demo    time.Duration
	step    string
}

// New собирает вид reminder: обработчик задач и фоновый диспетчер.
func New(d core.Deps, params yaml.Node) (core.Instance, error) {
	var p Params
	if err := params.Decode(&p); err != nil {
		return core.Instance{}, fmt.Errorf("параметры reminder: %w", err)
	}
	if len(p.Offsets) == 0 {
		return core.Instance{}, fmt.Errorf("параметры reminder: offsets пуст")
	}
	h := &Handler{channel: p.Channel, clock: d.Clock, loc: d.Loc, demo: d.ReminderDemoDelay, step: d.Step}
	if h.channel == "" {
		h.channel = "sms"
	}
	for _, s := range p.Offsets {
		off, err := ParseOffset(s)
		if err != nil {
			return core.Instance{}, fmt.Errorf("параметры reminder: %w", err)
		}
		h.offsets = append(h.offsets, off)
	}
	h.disp = dispatch.New(dispatch.Config{DueKey: DueKey, ItemPrefix: ItemPrefix, StatsKey: stats.Key("reminder")},
		d.Store, d.Clock, dispatch.NotifyDeliver(d.Sink, d.Clock, "reminder"), d.Log)
	return core.Instance{Tasks: h, Background: []func(context.Context){h.disp.Run}}, nil
}

func (h *Handler) Handle(ctx context.Context, t core.Task) (any, error) {
	pl, err := core.ParsePayload(t.Payload)
	if err != nil {
		return nil, core.Permanent("некорректный payload: %v", err)
	}
	var (
		apptID, startsAtStr, endsAtStr, doctorName, specialty string
		patientID, patientName, phone                         string
	)
	pl.Get("appointment_id", &apptID, 2, 0, 1)
	pl.Get("starts_at", &startsAtStr, 2, 0, 1)
	pl.Get("ends_at", &endsAtStr, 2, 0, 1)
	pl.Get("doctor_name", &doctorName, 2, 0, 1)
	pl.Get("specialty", &specialty, 2, 0, 1)
	pl.Get("patient_id", &patientID, 1, 0)
	pl.Get("patient_name", &patientName, 1, 0)
	pl.Get("phone", &phone, 1, 0)

	startsAt, err := time.Parse(time.RFC3339, startsAtStr)
	if err != nil {
		return nil, core.Permanent("в previous нет корректного starts_at (запись не создана?)")
	}
	startsAt = startsAt.UTC()
	now := h.clock.Now()
	text := fmt.Sprintf("Напоминание о приёме: %s (%s), %s.", doctorName, directory.Title(specialty),
		startsAt.In(h.loc).Format("02.01.2006 15:04 MST"))

	plans := Schedule(startsAt, now, h.offsets, h.demo)
	if apptID != "" {
		for i := range plans {
			plans[i].ID = core.StableID("reminder", apptID, strconv.Itoa(i))
		}
	}
	out := make([]map[string]any, 0, len(plans))
	for _, p := range plans {
		fields := map[string]string{
			"reminder_id": p.ID, "run_id": t.RunID, "appointment_id": apptID,
			"patient_id": patientID, "patient_name": patientName, "phone": phone,
			"channel": h.channel, "text": text, "starts_at": startsAt.Format(time.RFC3339),
			"ends_at": endsAtStr,
		}
		if err := h.disp.Schedule(ctx, p.ID, p.SendAt, fields); err != nil {
			return nil, core.Transient("redis: %v", err)
		}
		out = append(out, map[string]any{
			"reminder_id": p.ID, "send_at": p.SendAt.Format(time.RFC3339), "channel": h.channel, "status": "scheduled",
		})
	}
	// appointment_id/starts_at/ends_at сверх контракта: без них feedback не узнает время конца приёма.
	return map[string]any{
		"reminders": out, "appointment_id": apptID,
		"starts_at": startsAt.Format(time.RFC3339), "ends_at": endsAtStr,
	}, nil
}
