package autoscale

import (
	"testing"
	"time"
)

func TestDesiredTable(t *testing.T) {
	t.Parallel()
	t0 := time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)
	cooldown := 30 * time.Second
	cases := []struct {
		name       string
		backlog    int
		current    int
		lastGrowth time.Time
		now        time.Time
		p          Params
		want       int
	}{
		{
			name:    "нулевая очередь при min=1 — держим min",
			backlog: 0, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 1,
		},
		{
			name:    "рост сразу: 11 задач при 5 на экземпляр = 3",
			backlog: 11, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 3,
		},
		{
			name:    "ровно кратно: 5 задач = 1 экземпляр",
			backlog: 5, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 1,
		},
		{
			name:    "на единицу больше кратного: 6 задач = 2",
			backlog: 6, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 2,
		},
		{
			name:    "рост ограничен Max",
			backlog: 100, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 3,
		},
		{
			name:    "уменьшение не раньше cooldown — держим текущее",
			backlog: 0, current: 3, lastGrowth: t0, now: t0.Add(10 * time.Second),
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 3,
		},
		{
			name:    "уменьшение после cooldown до min",
			backlog: 0, current: 3, lastGrowth: t0, now: t0.Add(cooldown),
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 1,
		},
		{
			name:    "уменьшение до желаемого, а не до min, если очередь ещё есть",
			backlog: 6, current: 3, lastGrowth: t0, now: t0.Add(cooldown),
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 2,
		},
		{
			name:    "превышение Max исправляется сразу даже в cooldown",
			backlog: 0, current: 5, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 3,
		},
		{
			name:    "min задаёт пол, даже если очередь пуста",
			backlog: 0, current: 2, lastGrowth: t0, now: t0.Add(cooldown),
			p:    Params{PerInstance: 5, Min: 2, Max: 4, Cooldown: cooldown},
			want: 2,
		},
		{
			name:    "отрицательный backlog трактуется как 0",
			backlog: -7, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: cooldown},
			want: 1,
		},
		{
			name:    "per_instance < 1 трактуется как 1",
			backlog: 4, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 0, Min: 1, Max: 10, Cooldown: cooldown},
			want: 4,
		},
		{
			name:    "Max=0 — без верхней границы",
			backlog: 50, current: 1, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 0, Cooldown: cooldown},
			want: 10,
		},
		{
			name:    "уменьшение без cooldown (0) сразу",
			backlog: 0, current: 3, lastGrowth: t0, now: t0,
			p:    Params{PerInstance: 5, Min: 1, Max: 3, Cooldown: 0},
			want: 1,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			got := Desired(c.backlog, c.current, c.lastGrowth, c.now, c.p)
			if got != c.want {
				t.Fatalf("Desired = %d, ожидалось %d", got, c.want)
			}
		})
	}
}

func TestParamsFromStepConfig(t *testing.T) {
	t.Parallel()
	sc := StepConfig{Step: "triage", PerInstance: 5, Min: 1, Max: 3, Cooldown: time.Second}
	p := sc.Params()
	if p.PerInstance != 5 || p.Min != 1 || p.Max != 3 || p.Cooldown != time.Second {
		t.Fatalf("Params = %+v", p)
	}
}
