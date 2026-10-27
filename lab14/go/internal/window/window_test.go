package window

import (
	"encoding/json"
	"math"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

const (
	size     = 10 * time.Second
	lateness = 2 * time.Second
)

// base — граница окна (кратна 10 с по эпохе).
var base = time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)

func fp(v float64) *float64 { return &v }

func reading(seq int64, ts time.Time, metric string, v *float64) model.Reading {
	return model.Reading{DeviceID: "dev-0001", PatientID: "p-0001", Seq: seq, Ts: ts, Metric: metric, Value: v, Unit: "bpm"}
}

func findAgg(t *testing.T, aggs []model.Agg, start time.Time, metric string) model.Agg {
	t.Helper()
	for _, a := range aggs {
		if a.WindowStart.Equal(start) && a.Metric == metric {
			return a
		}
	}
	t.Fatalf("окно %v/%s не найдено (получено %d окон)", start, metric, len(aggs))
	return model.Agg{}
}

func TestHalfOpenBounds(t *testing.T) {
	cases := []struct {
		name      string
		ts        time.Time
		wantStart time.Time
	}{
		{"ровно начало окна", base, base},
		{"за 1 мс до конца", base.Add(size - time.Millisecond), base},
		{"ровно конец — следующее окно", base.Add(size), base.Add(size)},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			a := New("c1", size, lateness)
			now := c.ts
			if a.Add(reading(1, c.ts, "heart_rate", fp(70)), now) != Accepted {
				t.Fatal("показание не принято")
			}
			aggs := a.CloseAll(now)
			findAgg(t, aggs, c.wantStart, "heart_rate")
		})
	}
}

func TestAlignmentToEpoch(t *testing.T) {
	// ts внутри 10-секундного окна эпохи: окно начинается с кратного 10 с
	ts := time.Date(2026, 10, 13, 9, 30, 17, 250_000_000, time.UTC)
	a := New("c1", size, lateness)
	a.Add(reading(1, ts, "spo2", fp(97)), ts)
	aggs := a.CloseAll(ts)
	if len(aggs) != 1 {
		t.Fatalf("окон %d", len(aggs))
	}
	want := time.Date(2026, 10, 13, 9, 30, 10, 0, time.UTC)
	if !aggs[0].WindowStart.Equal(want) || !aggs[0].WindowEnd.Equal(want.Add(size)) {
		t.Fatalf("окно [%v, %v), ожидалось [%v, %v)", aggs[0].WindowStart, aggs[0].WindowEnd, want, want.Add(size))
	}
}

func TestCloseByEndPlusLateness(t *testing.T) {
	a := New("c1", size, lateness)
	a.Add(reading(1, base.Add(time.Second), "heart_rate", fp(80)), base.Add(time.Second))
	closeAt := base.Add(size + lateness) // ровно window_end + lateness

	if got := a.CloseDue(closeAt.Add(-time.Millisecond)); len(got) != 0 {
		t.Fatalf("окно закрыто раньше срока: %d", len(got))
	}
	got := a.CloseDue(closeAt)
	if len(got) != 1 || got[0].Count != 1 {
		t.Fatalf("на window_end+lateness ожидалось закрытие 1 окна, получено %d", len(got))
	}
	if a.Stats().EarlyClosed != 0 {
		t.Fatal("штатное закрытие не должно считаться досрочным")
	}
}

func TestLateDropped(t *testing.T) {
	a := New("c1", size, lateness)
	a.CloseDue(base.Add(size + lateness))
	now := base.Add(size + lateness + time.Second)
	if res := a.Add(reading(5, base.Add(time.Second), "heart_rate", fp(75)), now); res != Late {
		t.Fatalf("опоздавшее показание: результат %v, ожидался Late", res)
	}
	if a.Stats().LateDropped != 1 {
		t.Fatalf("late_dropped = %d, ожидалось 1", a.Stats().LateDropped)
	}
}

func TestAggregates(t *testing.T) {
	a := New("c1", size, lateness)
	now := base.Add(5 * time.Second)
	vals := []*float64{fp(60), fp(90), nil, fp(999), fp(75)} // nil и 999 — невалидны
	for i, v := range vals {
		a.Add(reading(int64(i+1), base.Add(time.Duration(i)*time.Millisecond*100), "heart_rate", v), now)
	}
	aggs := a.CloseAll(now)
	g := findAgg(t, aggs, base, "heart_rate")
	if g.Count != 3 || g.Invalid != 2 {
		t.Fatalf("count=%d invalid=%d, ожидалось 3 и 2", g.Count, g.Invalid)
	}
	if g.Sum != 225 || g.Min != 60 || g.Max != 90 || g.Mean != 75 || g.Last != 75 {
		t.Fatalf("sum=%v min=%v max=%v mean=%v last=%v", g.Sum, g.Min, g.Max, g.Mean, g.Last)
	}
}

func TestWindowWithoutValidValues(t *testing.T) {
	a := New("c1", size, lateness)
	now := base.Add(time.Second)
	a.Add(reading(1, base, "spo2", nil), now)
	a.Add(reading(2, base.Add(time.Millisecond), "spo2", fp(-1)), now)
	g := findAgg(t, a.CloseAll(now), base, "spo2")
	if g.Count != 0 || g.Invalid != 2 {
		t.Fatalf("count=%d invalid=%d", g.Count, g.Invalid)
	}
	if g.Sum != 0 {
		t.Fatalf("sum=%v, ожидался 0", g.Sum)
	}
	b, err := json.Marshal(g)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	for _, k := range []string{"min", "max", "mean", "last"} {
		if m[k] != nil {
			t.Errorf("%s = %v, ожидался null при count=0", k, m[k])
		}
	}
}

// Будущий ts (часы устройства) не должен попасть в окно из будущего: показание
// учитывается как невалидное в текущем окне сборщика.
func TestFutureTimestampCountedInvalid(t *testing.T) {
	a := New("c1", size, lateness)
	now := base.Add(time.Second)
	future := now.Add(time.Hour)
	if res := a.Add(reading(1, future, "heart_rate", fp(70)), now); res != Accepted {
		t.Fatalf("результат %v", res)
	}
	g := findAgg(t, a.CloseAll(now), base, "heart_rate")
	if g.Invalid != 1 || g.Count != 0 {
		t.Fatalf("count=%d invalid=%d, ожидалось 0 и 1", g.Count, g.Invalid)
	}
	if a.Stats().FutureClamped != 1 {
		t.Fatalf("future_clamped=%d", a.Stats().FutureClamped)
	}
}

// НАХОДКА: контракт (ARCHITECTURE.md, раздел 4) говорит, что дубликаты сборщик не
// отфильтровывает — это задача очистки в Python. Window же молча отбрасывает любой
// seq, не превышающий последний, и повторное показание не попадает в count.
// Генератор тестов ожидал двойного учёта повтора (count = 2), ссылаясь на фразу раздела 4
// «дубликаты сборщик не отфильтровывает». Она относится к сырому потоку: raw уходит как есть,
// а очистка в Python убирает дубликаты. В агрегате двойной учёт исказил бы count, sum и mean,
// поэтому окно учитывает показание с данным seq один раз. Фраза контракта уточнена.
func TestDuplicateSeqCountedOnceInWindow(t *testing.T) {
	a := New("c1", size, lateness)
	now := base.Add(time.Second)
	r := reading(7, base, "heart_rate", fp(70))
	a.Add(r, now)
	a.Add(r, now) // повторная выдача того же seq
	g := findAgg(t, a.CloseAll(now), base, "heart_rate")
	if g.Count != 1 || g.Sum != 70 {
		t.Fatalf("повтор seq учтён в окне дважды: count=%d sum=%v", g.Count, g.Sum)
	}
}

func TestNaNIsInvalid(t *testing.T) {
	a := New("c1", size, lateness)
	now := base.Add(time.Second)
	a.Add(reading(1, base, "temperature", fp(math.NaN())), now)
	g := findAgg(t, a.CloseAll(now), base, "temperature")
	if g.Invalid != 1 || g.Count != 0 {
		t.Fatalf("NaN: count=%d invalid=%d", g.Count, g.Invalid)
	}
}
