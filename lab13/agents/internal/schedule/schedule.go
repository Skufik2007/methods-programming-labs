// Пакет schedule — чистая логика расписания врача: смена, сетка слотов, пересечения,
// свободные слоты.
//
// Логика перенесена с Python (ЛР №12, app/services/scheduling.py) и переписана под Go:
// те же правила (сетка от начала смены, полуоткрытые интервалы [start, end),
// окно записи, последний неполный слот отбрасывается), но смены задаются смещениями
// от местной полуночи, а время клиники берётся через time.LoadLocation
// (по умолчанию Europe/Moscow; tzdata вшит в бинарник).
//
// Пакет не читает системные часы: «сейчас» всегда приходит параметром.
package schedule

import (
	"fmt"
	"time"
	_ "time/tzdata" // чтобы Europe/Moscow находился и в минимальных образах без tzdata
)

const (
	MaxBookingDaysAhead    = 90 // запись не дальше чем на столько суток вперёд
	MaxSlotsPerAppointment = 4  // длительность записи — от 1 до 4 слотов
	defaultLocationName    = "Europe/Moscow"
	isoDaysCount           = 7
	timeFormatHM           = "15:04"
)

// Error — нарушено правило расписания.
type Error struct{ Msg string }

func (e *Error) Error() string { return e.Msg }

func errf(format string, a ...any) error { return &Error{Msg: fmt.Sprintf(format, a...)} }

// Schedule — график врача. Время смены — смещение от местной полуночи.
type Schedule struct {
	workDays    [isoDaysCount + 1]bool // индекс — ISO-день, 1 = понедельник
	WorkStart   time.Duration
	WorkEnd     time.Duration
	SlotMinutes int
}

// Slot — полуоткрытый интервал [Start, End), всегда в UTC.
type Slot struct {
	Start time.Time
	End   time.Time
}

// Interval — занятый интервал.
type Interval = Slot

// LoadLocation загружает часовой пояс клиники; пустое имя — Europe/Moscow.
func LoadLocation(name string) (*time.Location, error) {
	if name == "" {
		name = defaultLocationName
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return nil, fmt.Errorf("часовой пояс %q: %w", name, err)
	}
	return loc, nil
}

// New проверяет и собирает график. start/end — «ЧЧ:ММ».
func New(workDays []int, start, end string, slotMinutes int) (Schedule, error) {
	var s Schedule
	if slotMinutes <= 0 {
		return s, fmt.Errorf("slot_minutes должен быть положительным")
	}
	ws, err := parseHM(start)
	if err != nil {
		return s, fmt.Errorf("work_start: %w", err)
	}
	we, err := parseHM(end)
	if err != nil {
		return s, fmt.Errorf("work_end: %w", err)
	}
	if ws >= we {
		return s, fmt.Errorf("work_start должен быть раньше work_end")
	}
	if len(workDays) == 0 {
		return s, fmt.Errorf("work_days пуст")
	}
	for _, d := range workDays {
		if d < 1 || d > isoDaysCount {
			return s, fmt.Errorf("work_days: ISO-день %d вне 1..7", d)
		}
		s.workDays[d] = true
	}
	s.WorkStart, s.WorkEnd, s.SlotMinutes = ws, we, slotMinutes
	return s, nil
}

func parseHM(v string) (time.Duration, error) {
	t, err := time.Parse(timeFormatHM, v)
	if err != nil {
		return 0, fmt.Errorf("ожидается ЧЧ:ММ, получено %q", v)
	}
	return time.Duration(t.Hour())*time.Hour + time.Duration(t.Minute())*time.Minute, nil
}

func (s Schedule) Works(isoDay int) bool {
	return isoDay >= 1 && isoDay <= isoDaysCount && s.workDays[isoDay]
}

func (s Schedule) step() time.Duration { return time.Duration(s.SlotMinutes) * time.Minute }

func isoWeekday(t time.Time) int {
	if w := int(t.Weekday()); w != 0 {
		return w
	}
	return 7
}

// sinceMidnight — «настенное» время суток как смещение от местной полуночи.
func sinceMidnight(t time.Time) time.Duration {
	return time.Duration(t.Hour())*time.Hour + time.Duration(t.Minute())*time.Minute +
		time.Duration(t.Second())*time.Second + time.Duration(t.Nanosecond())
}

// wall собирает момент по настенным часам местной даты y-m-d плюс смещение.
// time.Date нормализует минуты, так что переход через час не ломается.
func wall(day time.Time, off time.Duration, loc *time.Location) time.Time {
	y, m, d := day.In(loc).Date()
	return time.Date(y, m, d, 0, int(off/time.Minute), 0, 0, loc)
}

// DaySlots — все слоты сетки на местную дату day (без учёта занятости и «сейчас»).
func DaySlots(s Schedule, day time.Time, loc *time.Location) []Slot {
	if !s.Works(isoWeekday(day.In(loc))) {
		return nil
	}
	step := s.step()
	var out []Slot
	for off := s.WorkStart; off+step <= s.WorkEnd; off += step {
		out = append(out, Slot{
			Start: wall(day, off, loc).UTC(),
			End:   wall(day, off+step, loc).UTC(),
		})
	}
	return out
}

// Overlaps: пересекаются ли полуоткрытые интервалы; касание границами — не пересечение.
func Overlaps(aStart, aEnd, bStart, bEnd time.Time) bool {
	return aStart.Before(bEnd) && bStart.Before(aEnd)
}

// FreeSlots — свободные слоты на местную дату day: слот начинается строго после now,
// не позже now+maxDaysAhead суток и не пересекается с busy. Порядок — по возрастанию.
func FreeSlots(s Schedule, day time.Time, loc *time.Location, busy []Interval, now time.Time, maxDaysAhead int) []Slot {
	latest := now.AddDate(0, 0, maxDaysAhead)
	var out []Slot
slots:
	for _, sl := range DaySlots(s, day, loc) {
		if !sl.Start.After(now) || sl.Start.After(latest) {
			continue
		}
		for _, b := range busy {
			if Overlaps(sl.Start, sl.End, b.Start, b.End) {
				continue slots
			}
		}
		out = append(out, sl)
	}
	return out
}

// ValidateBookingWindow: запись только в будущее и не дальше maxDaysAhead суток.
func ValidateBookingWindow(start, now time.Time, maxDaysAhead int) error {
	if !start.After(now) {
		return errf("запись должна начинаться в будущем")
	}
	if start.After(now.AddDate(0, 0, maxDaysAhead)) {
		return errf("запись возможна не дальше чем на %d суток вперёд", maxDaysAhead)
	}
	return nil
}

// ValidateWithinShift: интервал целиком в одном рабочем дне и в смене врача.
func ValidateWithinShift(start, end time.Time, s Schedule, loc *time.Location) error {
	if !end.After(start) {
		return errf("конец записи должен быть позже начала")
	}
	ls, le := start.In(loc), end.In(loc)
	if ls.Format("2006-01-02") != le.Format("2006-01-02") {
		return errf("запись не может переходить через полночь")
	}
	if !s.Works(isoWeekday(ls)) {
		return errf("врач не работает в этот день недели")
	}
	if sinceMidnight(ls) < s.WorkStart || sinceMidnight(le) > s.WorkEnd {
		return errf("запись должна укладываться в смену врача")
	}
	return nil
}

// ValidateSlotGrid: начало совпадает с границей сетки (work_start + k·шаг), длительность —
// целое число слотов от 1 до MaxSlotsPerAppointment.
func ValidateSlotGrid(start, end time.Time, s Schedule, loc *time.Location) error {
	step := s.step()
	off := sinceMidnight(start.In(loc)) - s.WorkStart
	if off < 0 || off%step != 0 {
		return errf("запись должна начинаться на границе слота (каждые %d мин от начала смены)", s.SlotMinutes)
	}
	n, rem := end.Sub(start)/step, end.Sub(start)%step
	if rem != 0 || n < 1 || n > MaxSlotsPerAppointment {
		return errf("длительность записи — от 1 до %d слотов по %d мин", MaxSlotsPerAppointment, s.SlotMinutes)
	}
	return nil
}
