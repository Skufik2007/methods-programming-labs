package schedule

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// Тестовая смена: терапевт, пн–пт, 08:00–14:00, слот 20 мин (roles/booking-a.md).
func therapistSchedule(t *testing.T) Schedule {
	t.Helper()
	s, err := New([]int{1, 2, 3, 4, 5}, "08:00", "14:00", 20)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return s
}

func moscow(t *testing.T) *time.Location {
	t.Helper()
	loc, err := LoadLocation("Europe/Moscow")
	if err != nil {
		t.Fatal(err)
	}
	return loc
}

// msk — момент по местному времени Москвы.
func msk(t *testing.T, y int, m time.Month, d, h, min int) time.Time {
	t.Helper()
	return time.Date(y, m, d, h, min, 0, 0, moscow(t))
}

// 2026-10-13 — вторник (ISO 2); 2026-10-17 — суббота.
func TestNewValidation(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		days     []int
		start    string
		end      string
		slot     int
		wantSubs string
	}{
		{"нулевой слот", []int{1}, "08:00", "14:00", 0, "slot_minutes"},
		{"отрицательный слот", []int{1}, "08:00", "14:00", -20, "slot_minutes"},
		{"начало не ЧЧ:ММ", []int{1}, "8am", "14:00", 20, "work_start"},
		{"конец не ЧЧ:ММ", []int{1}, "08:00", "25:99", 20, "work_end"},
		{"начало позже конца", []int{1}, "14:00", "08:00", 20, "раньше work_end"},
		{"начало равно концу", []int{1}, "08:00", "08:00", 20, "раньше work_end"},
		{"пустые рабочие дни", nil, "08:00", "14:00", 20, "пуст"},
		{"день 0", []int{0}, "08:00", "14:00", 20, "вне 1..7"},
		{"день 8", []int{8}, "08:00", "14:00", 20, "вне 1..7"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, err := New(c.days, c.start, c.end, c.slot)
			if err == nil {
				t.Fatalf("ожидалась ошибка с %q", c.wantSubs)
			}
			if !strings.Contains(err.Error(), c.wantSubs) {
				t.Fatalf("ошибка %q без %q", err, c.wantSubs)
			}
		})
	}
}

func TestLoadLocation(t *testing.T) {
	t.Parallel()
	loc, err := LoadLocation("")
	if err != nil || loc.String() != "Europe/Moscow" {
		t.Fatalf("пустое имя: loc=%v err=%v", loc, err)
	}
	if _, err := LoadLocation("Mars/Olympus"); err == nil {
		t.Fatal("неизвестный пояс должен давать ошибку")
	}
}

func TestDaySlotsGrid(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	loc := moscow(t)
	day := msk(t, 2026, time.October, 13, 12, 0) // вторник

	slots := DaySlots(s, day, loc)
	if len(slots) != 18 { // 6 часов по 20 мин
		t.Fatalf("слотов %d, ожидалось 18", len(slots))
	}
	first, last := slots[0], slots[len(slots)-1]
	if want := msk(t, 2026, time.October, 13, 8, 0).UTC(); !first.Start.Equal(want) {
		t.Errorf("первый старт %v, ожидалось %v", first.Start, want)
	}
	if want := msk(t, 2026, time.October, 13, 14, 0).UTC(); !last.End.Equal(want) {
		t.Errorf("последний конец %v, ожидалось %v", last.End, want)
	}
	for i := 1; i < len(slots); i++ {
		if !slots[i].Start.Equal(slots[i-1].End) {
			t.Fatalf("слоты %d и %d не стыкуются", i-1, i)
		}
	}
}

func TestDaySlotsDropsIncompleteLastSlot(t *testing.T) {
	t.Parallel()
	// 08:00–14:10 при слоте 20 мин: 14:00–14:20 не помещается целиком — отбрасывается.
	s, err := New([]int{2}, "08:00", "14:10", 20)
	if err != nil {
		t.Fatal(err)
	}
	slots := DaySlots(s, msk(t, 2026, time.October, 13, 9, 0), moscow(t))
	if len(slots) != 18 {
		t.Fatalf("слотов %d, ожидалось 18 (последний неполный отброшен)", len(slots))
	}
	for _, sl := range slots {
		if sl.End.After(msk(t, 2026, time.October, 13, 14, 10).UTC()) {
			t.Fatalf("слот %v выходит за конец смены", sl)
		}
	}
}

func TestDaySlotsNonWorkingDay(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	sat := msk(t, 2026, time.October, 17, 12, 0)
	if got := DaySlots(s, sat, moscow(t)); got != nil {
		t.Fatalf("в субботу слотов быть не должно, получено %d", len(got))
	}
}

// Дата определяется по местному времени: 00:30 по Москве 14 октября — это 21:30 UTC 13 октября.
func TestDaySlotsUsesLocalDate(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	loc := moscow(t)
	// 2026-10-13T21:30Z — это 2026-10-14 00:30 по Москве (среда): слоты берутся для среды.
	utcInstant := time.Date(2026, time.October, 13, 21, 30, 0, 0, time.UTC)
	slots := DaySlots(s, utcInstant, loc)
	if len(slots) != 18 {
		t.Fatalf("ожидались слоты рабочего дня (среда), получено %d", len(slots))
	}
	if want := msk(t, 2026, time.October, 14, 8, 0).UTC(); !slots[0].Start.Equal(want) {
		t.Errorf("первый слот %v, ожидалось 08:00 MSK 14.10 = %v", slots[0].Start, want)
	}
}

func TestOverlapsHalfOpen(t *testing.T) {
	t.Parallel()
	base := time.Date(2026, 10, 13, 9, 0, 0, 0, time.UTC)
	at := func(m int) time.Time { return base.Add(time.Duration(m) * time.Minute) }
	cases := []struct {
		name       string
		aS, aE     int
		bS, bE     int
		wantOverlp bool
	}{
		{"полное совпадение", 0, 20, 0, 20, true},
		{"вложенный", 0, 30, 10, 20, true},
		{"частичное пересечение", 0, 20, 10, 30, true},
		{"касание справа: [0,20) и [20,40)", 0, 20, 20, 40, false},
		{"касание слева: [20,40) и [0,20)", 20, 40, 0, 20, false},
		{"разнесены", 0, 20, 40, 60, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			got := Overlaps(at(c.aS), at(c.aE), at(c.bS), at(c.bE))
			if got != c.wantOverlp {
				t.Fatalf("Overlaps = %v, ожидалось %v", got, c.wantOverlp)
			}
		})
	}
}

func TestFreeSlotsBusyAndTouching(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	loc := moscow(t)
	day := msk(t, 2026, time.October, 13, 12, 0)
	now := msk(t, 2026, time.October, 13, 7, 0) // до смены

	busy := []Interval{
		{Start: msk(t, 2026, time.October, 13, 9, 0).UTC(), End: msk(t, 2026, time.October, 13, 9, 20).UTC()},
	}
	free := FreeSlots(s, day, loc, busy, now, 14)
	for _, f := range free {
		if f.Start.Equal(busy[0].Start) {
			t.Fatalf("занятый слот 09:00 предложен как свободный")
		}
	}
	if len(free) != 17 {
		t.Fatalf("свободных %d, ожидалось 17 (18 минус один занятый)", len(free))
	}

	// Слоты 08:40–09:00 и 09:20–09:40 касаются занятости [09:00,09:20) границами — они свободны.
	has := func(list []Slot, start time.Time) bool {
		for _, sl := range list {
			if sl.Start.Equal(start) {
				return true
			}
		}
		return false
	}
	if !has(free, msk(t, 2026, time.October, 13, 8, 40).UTC()) {
		t.Errorf("слот 08:40 касается занятости границей и должен быть свободен")
	}
	if !has(free, msk(t, 2026, time.October, 13, 9, 20).UTC()) {
		t.Errorf("слот 09:20 касается занятости границей и должен быть свободен")
	}
}

func TestFreeSlotsPastAndHorizon(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	loc := moscow(t)
	day := msk(t, 2026, time.October, 13, 12, 0)

	// now ровно 10:00: слот, начинающийся в 10:00, уже прошёл (строго после now).
	now := msk(t, 2026, time.October, 13, 10, 0)
	free := FreeSlots(s, day, loc, nil, now, 14)
	if len(free) == 0 || !free[0].Start.Equal(msk(t, 2026, time.October, 13, 10, 20).UTC()) {
		t.Fatalf("первый свободный слот должен быть 10:20, получено %v", free[0].Start)
	}

	// Горизонт 0 суток: latest == now, все слоты дня начинаются после now, но не позже now — пусто.
	if got := FreeSlots(s, day, loc, nil, now, 0); len(got) != 0 {
		t.Fatalf("при горизонте 0 суток свободных слотов быть не должно, получено %d", len(got))
	}

	// День полностью в прошлом.
	if got := FreeSlots(s, day, loc, nil, msk(t, 2026, time.October, 14, 0, 0), 14); len(got) != 0 {
		t.Fatalf("прошедший день: свободных %d", len(got))
	}

	// Порядок — по возрастанию.
	all := FreeSlots(s, day, loc, nil, msk(t, 2026, time.October, 13, 7, 0), 14)
	for i := 1; i < len(all); i++ {
		if !all[i].Start.After(all[i-1].Start) {
			t.Fatalf("порядок нарушен на %d", i)
		}
	}
}

func TestValidateBookingWindow(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)
	if err := ValidateBookingWindow(now, now, 90); err == nil {
		t.Error("запись на текущий момент должна быть запрещена")
	}
	if err := ValidateBookingWindow(now.Add(-time.Minute), now, 90); err == nil {
		t.Error("запись в прошлое должна быть запрещена")
	}
	if err := ValidateBookingWindow(now.Add(time.Minute), now, 90); err != nil {
		t.Errorf("запись через минуту допустима: %v", err)
	}
	edge := now.AddDate(0, 0, 90)
	if err := ValidateBookingWindow(edge, now, 90); err != nil {
		t.Errorf("ровно 90 суток допустимо: %v", err)
	}
	if err := ValidateBookingWindow(edge.Add(time.Second), now, 90); err == nil {
		t.Error("дальше 90 суток запрещено")
	}
}

func TestValidateWithinShift(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	loc := moscow(t)
	mk := func(y int, m time.Month, d, h, min int) time.Time { return msk(t, y, m, d, h, min) }
	cases := []struct {
		name       string
		start, end time.Time
		wantErr    bool
	}{
		{"начало смены", mk(2026, 10, 13, 8, 0), mk(2026, 10, 13, 8, 20), false},
		{"конец смены впритык", mk(2026, 10, 13, 13, 40), mk(2026, 10, 13, 14, 0), false},
		{"до начала смены", mk(2026, 10, 13, 7, 40), mk(2026, 10, 13, 8, 0), true},
		{"после конца смены", mk(2026, 10, 13, 14, 0), mk(2026, 10, 13, 14, 20), true},
		{"суббота", mk(2026, 10, 17, 9, 0), mk(2026, 10, 17, 9, 20), true},
		{"конец раньше начала", mk(2026, 10, 13, 9, 0), mk(2026, 10, 13, 8, 0), true},
		{"пустой интервал", mk(2026, 10, 13, 9, 0), mk(2026, 10, 13, 9, 0), true},
		{"переход через полночь", mk(2026, 10, 13, 23, 50), mk(2026, 10, 14, 0, 10), true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			err := ValidateWithinShift(c.start, c.end, s, loc)
			if (err != nil) != c.wantErr {
				t.Fatalf("err = %v, ожидалось ошибки: %v", err, c.wantErr)
			}
		})
	}
	var e *Error
	if err := ValidateWithinShift(mk(2026, 10, 17, 9, 0), mk(2026, 10, 17, 9, 20), s, loc); !errors.As(err, &e) {
		t.Errorf("ошибка должна быть типа *schedule.Error")
	}
}

func TestValidateSlotGrid(t *testing.T) {
	t.Parallel()
	s := therapistSchedule(t)
	loc := moscow(t)
	mk := func(h, min int) time.Time { return msk(t, 2026, time.October, 13, h, min) }
	cases := []struct {
		name       string
		start, end time.Time
		wantErr    bool
	}{
		{"один слот на границе", mk(8, 0), mk(8, 20), false},
		{"два слота", mk(8, 20), mk(8, 60), false},
		{"четыре слота — максимум", mk(9, 0), mk(10, 20), false},
		{"пять слотов — больше максимума", mk(9, 0), mk(10, 40), true},
		{"не на границе слота (08:10)", mk(8, 10), mk(8, 30), true},
		{"до начала смены", mk(7, 40), mk(8, 0), true},
		{"длительность не кратна слоту (30 мин)", mk(8, 0), mk(8, 30), true},
		{"нулевая длительность", mk(8, 0), mk(8, 0), true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			err := ValidateSlotGrid(c.start, c.end, s, loc)
			if (err != nil) != c.wantErr {
				t.Fatalf("err = %v, ожидалось ошибки: %v", err, c.wantErr)
			}
		})
	}
}
