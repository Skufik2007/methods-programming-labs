package booking

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

// Текущий момент: вторник 2026-10-13, 10:00 по Москве (= 07:00 UTC).
var now = time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)

func fixedClock() core.Clock { return func() time.Time { return now } }

// Терапевт (пн–пт 08:00–14:00, слот 20 мин) и ЛОР (пн/ср/пт 09:00–17:00, слот 30 мин).
func testParams() Params {
	return Params{
		MinLeadMinutes: 30,
		HorizonDays:    14,
		Doctors: []DoctorCfg{
			{ID: "d-therapist-1", Name: "Смирнова", Specialty: "therapist", WorkDays: []int{1, 2, 3, 4, 5},
				WorkStart: "08:00", WorkEnd: "14:00", SlotMinutes: 20},
			{ID: "d-ent-1", Name: "Лебедева", Specialty: "ent", WorkDays: []int{1, 3, 5},
				WorkStart: "09:00", WorkEnd: "17:00", SlotMinutes: 30},
		},
	}
}

func moscowLoc(t *testing.T) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation("Europe/Moscow")
	if err != nil {
		t.Fatal(err)
	}
	return loc
}

func newSvc(t *testing.T, p Params, st store.Store) *Service {
	t.Helper()
	svc, err := NewService("booking-test", p, moscowLoc(t), st, fixedClock())
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func TestSkill(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name                   string
		doctor, requested, urg string
		want                   float64
	}{
		{"совпадение специальности", "cardiologist", "cardiologist", "routine", 1},
		{"терапевт для терапевта", "therapist", "therapist", "urgent", 1},
		{"терапевт вместо кардиолога, плановый", "therapist", "cardiologist", "routine", 0.5},
		{"терапевт вместо ЛОРа, срочный — не годится", "therapist", "ent", "urgent", 0},
		{"кардиолог вместо терапевта", "cardiologist", "therapist", "routine", 0},
		{"невролог вместо ЛОРа", "neurologist", "ent", "routine", 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if got := Skill(c.doctor, c.requested, c.urg); got != c.want {
				t.Fatalf("Skill = %v, ожидалось %v", got, c.want)
			}
		})
	}
}

// Формула из раздела 6: cost = 0.5·(1 - availability) + 0.3·load + 0.2·(1 - skill).
func TestCostFormulaNumbers(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name                      string
		availability, load, skill float64
		want                      float64
	}{
		{"идеальная ставка", 1, 0, 1, 0},
		{"доступность 0.83, загрузка 0.25, полный навык", 0.83, 0.25, 1, 0.16},
		{"только навык 0.5", 1, 0, 0.5, 0.1},
		{"худшая ставка", 0, 1, 0, 1},
		{"доступность 0.5, загрузка 0.5, навык 0.5", 0.5, 0.5, 0.5, 0.25 + 0.15 + 0.1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if got := Cost(c.availability, c.load, c.skill); math.Abs(got-c.want) > 1e-9 {
				t.Fatalf("Cost = %.6f, ожидалось %.6f", got, c.want)
			}
		})
	}
}

func TestBetterTieBreak(t *testing.T) {
	t.Parallel()
	a := Bid{Cost: 0.2, SlotStart: "2026-10-13T08:00:00Z", DoctorID: "d-b"}
	b := Bid{Cost: 0.3, SlotStart: "2026-10-13T07:00:00Z", DoctorID: "d-a"}
	if !better(a, b) || better(b, a) {
		t.Fatal("меньший cost должен побеждать независимо от слота")
	}
	c := Bid{Cost: 0.2, SlotStart: "2026-10-13T07:00:00Z", DoctorID: "d-z"}
	if !better(c, a) {
		t.Fatal("при равном cost раньше должен быть более ранний slot_start")
	}
	d := Bid{Cost: 0.2, SlotStart: "2026-10-13T08:00:00Z", DoctorID: "d-a"}
	if !better(d, a) {
		t.Fatal("при равных cost и slot_start — меньший doctor_id")
	}
}

func TestNewServiceValidation(t *testing.T) {
	t.Parallel()
	loc := moscowLoc(t)
	base := testParams
	cases := []struct {
		name    string
		mutate  func(*Params)
		wantErr string
	}{
		{"нет врачей", func(p *Params) { p.Doctors = nil }, "ни одного врача"},
		{"отрицательный lead", func(p *Params) { p.MinLeadMinutes = -1 }, "min_lead_minutes"},
		{"horizon больше 90", func(p *Params) { p.HorizonDays = 91 }, "horizon_days"},
		{"повторяющийся id", func(p *Params) { p.Doctors[1].ID = "d-therapist-1" }, "повторяющийся"},
		{"пустой id", func(p *Params) { p.Doctors[0].ID = "" }, "пустой"},
		{"специальность не из справочника", func(p *Params) { p.Doctors[0].Specialty = "dentist" }, "не из справочника"},
		{"неверная смена", func(p *Params) { p.Doctors[0].WorkStart = "25:00" }, "work_start"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			p := base()
			c.mutate(&p)
			_, err := NewService("x", p, loc, store.NewMemory(), fixedClock())
			if err == nil || !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("ошибка %v, ожидалось %q", err, c.wantErr)
			}
		})
	}
}

func TestNewServiceDefaultsAndDoctors(t *testing.T) {
	t.Parallel()
	p := testParams()
	p.HorizonDays = 0
	svc := newSvc(t, p, store.NewMemory())
	if svc.horizon != 14 {
		t.Errorf("horizon по умолчанию = %d, ожидалось 14", svc.horizon)
	}
	ids := svc.Doctors()
	if len(ids) != 2 || ids[0] != "d-ent-1" || ids[1] != "d-therapist-1" {
		t.Errorf("Doctors = %v", ids)
	}
}

func TestBidNumbersAndFormula(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	req := AuctionRequest{AuctionID: "a-1", RunID: "r-1", Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, Now: now.Format(time.RFC3339)}

	bid, err := svc.Bid(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if !bid.Eligible || bid.DoctorID != "d-therapist-1" {
		t.Fatalf("ставка: %+v", bid)
	}
	// Первый свободный слот не раньше now+30 мин (10:30) — ближайшее начало 10:40 MSK = 07:40 UTC.
	if bid.SlotStart != "2026-10-13T07:40:00Z" {
		t.Fatalf("slot_start = %s, ожидалось 2026-10-13T07:40:00Z", bid.SlotStart)
	}
	wantAvail := 1 - (40.0/60.0)/72.0
	if math.Abs(bid.Availability-wantAvail) > 1e-9 {
		t.Errorf("availability = %v, ожидалось %v", bid.Availability, wantAvail)
	}
	if bid.Load != 0 {
		t.Errorf("load без бронирований = %v, ожидалось 0", bid.Load)
	}
	if want := Cost(bid.Availability, bid.Load, bid.Skill); math.Abs(bid.Cost-want) > 1e-12 {
		t.Errorf("cost = %v, ожидалось по формуле %v", bid.Cost, want)
	}
	if math.Abs(bid.Cost-0.5*(1-wantAvail)) > 1e-9 {
		t.Errorf("cost = %v, ожидалось 0.5·(1-availability) = %v", bid.Cost, 0.5*(1-wantAvail))
	}
}

func TestBidLoadFromBookings(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	svc := newSvc(t, testParams(), st)
	loc := moscowLoc(t)
	// 9 занятых слотов терапевта на среду 14.10 с 08:00 до 10:40 MSK (≤ 7 суток от now).
	wed := time.Date(2026, 10, 14, 8, 0, 0, 0, loc)
	for i := 0; i < 9; i++ {
		start := wed.Add(time.Duration(i*20) * time.Minute).UTC()
		if err := st.ZAdd(context.Background(), BookedKey("d-therapist-1"), float64(start.Unix()),
			start.Format(time.RFC3339)); err != nil {
			t.Fatal(err)
		}
	}
	bid, err := svc.Bid(context.Background(), AuctionRequest{Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, Now: now.Format(time.RFC3339)})
	if err != nil {
		t.Fatal(err)
	}
	// Вместимость за 7 суток от 10:00 вт 13.10 до 10:00 вт 20.10 (по дате, включая текущий день):
	// вт 13.10 — 11 слотов после 10:00, ср 14, чт 15, пт 16, пн 19 — по 18, вт 20.10 — 7 (до 10:00).
	// Итого 90. Занято 9 — загрузка 0.1.
	if math.Abs(bid.Load-0.1) > 1e-9 {
		t.Fatalf("load = %v, ожидалось 0.1 (9 из 90 слотов)", bid.Load)
	}
}

func TestBidSkillZeroIsSkipped(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	// При плановом обращении терапевт даёт skill 0.5 любому специалисту; при срочном — 0.
	bid, err := svc.Bid(context.Background(), AuctionRequest{Specialty: "cardiologist", Urgency: "urgent",
		MaxWaitHours: 72, Now: now.Format(time.RFC3339)})
	if err != nil {
		t.Fatal(err)
	}
	if bid.Eligible || bid.Reason != "no_matching_doctor" {
		t.Fatalf("без подходящего врача ставка должна быть ineligible: %+v", bid)
	}
}

func TestBidUrgentTherapistNotForSpecialist(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	// Срочный запрос к ЛОРу: терапевт (skill 0.5 только при плановом) не участвует.
	// Ближайший слот ЛОРа — среда 09:00 (23 ч), поэтому допустимое ожидание 72 ч.
	bid, err := svc.Bid(context.Background(), AuctionRequest{Specialty: "ent", Urgency: "urgent",
		MaxWaitHours: 72, Now: now.Format(time.RFC3339)})
	if err != nil {
		t.Fatal(err)
	}
	if bid.DoctorID != "d-ent-1" || !bid.Eligible || bid.Skill != 1 {
		t.Fatalf("ожидалась ставка ЛОРа с skill 1: %+v", bid)
	}
}

func TestBidWaitExceedsMaximum(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	// Ближайший слот через 40 мин, а допустимо не больше 0.5 ч.
	bid, err := svc.Bid(context.Background(), AuctionRequest{Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 0.5, Now: now.Format(time.RFC3339)})
	if err != nil {
		t.Fatal(err)
	}
	if bid.Eligible || bid.Reason != "wait_exceeds_max" {
		t.Fatalf("ожидалось wait_exceeds_max: %+v", bid)
	}
	if bid.DoctorID != "d-therapist-1" {
		t.Errorf("ineligible-ставка должна называть врача: %q", bid.DoctorID)
	}
}

func TestBidNoFreeSlots(t *testing.T) {
	t.Parallel()
	p := testParams()
	p.HorizonDays = 1
	// Врач работает только по воскресеньям — в горизонте 1–2 суток слотов нет.
	p.Doctors = []DoctorCfg{{ID: "d-sun", Name: "Воскресный", Specialty: "therapist", WorkDays: []int{7},
		WorkStart: "08:00", WorkEnd: "14:00", SlotMinutes: 20}}
	svc := newSvc(t, p, store.NewMemory())
	bid, err := svc.Bid(context.Background(), AuctionRequest{Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, Now: now.Format(time.RFC3339)})
	if err != nil {
		t.Fatal(err)
	}
	if bid.Eligible || bid.Reason != "no_free_slots" {
		t.Fatalf("ожидалось no_free_slots: %+v", bid)
	}
}

func TestBidUsesRequestNowAndFallsBackToClock(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	// Поздний now в запросе: 13:00 MSK — слоты терапевта кончаются в 14:00, ближайший через 40 мин.
	late := time.Date(2026, 10, 13, 10, 0, 0, 0, time.UTC).Format(time.RFC3339)
	bid, err := svc.Bid(context.Background(), AuctionRequest{Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, Now: late})
	if err != nil {
		t.Fatal(err)
	}
	if bid.SlotStart != "2026-10-13T10:40:00Z" {
		t.Fatalf("slot_start %s — request.now не учтён", bid.SlotStart)
	}
	// Некорректный now — берётся часы сервиса.
	bid, err = svc.Bid(context.Background(), AuctionRequest{Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, Now: "не время"})
	if err != nil {
		t.Fatal(err)
	}
	if bid.SlotStart != "2026-10-13T07:40:00Z" {
		t.Fatalf("slot_start %s — ожидались часы сервиса", bid.SlotStart)
	}
}

// --- бронь ---

func bookReq(slot string) BookRequest {
	return BookRequest{AuctionID: "a-1", RunID: "r-1", DoctorID: "d-therapist-1", SlotStart: slot,
		PatientID: "p-001", PatientName: "Иванова Анна Петровна", Bids: 3, WinnerCost: 0.41}
}

func TestBookSuccessAndStoredRecord(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	svc := newSvc(t, testParams(), st)
	ctx := context.Background()

	res := svc.Book(ctx, bookReq("2026-10-13T07:40:00Z"))
	if !res.Success || res.Error != "" {
		t.Fatalf("бронь не прошла: %+v", res)
	}
	out, ok := res.Output.(map[string]any)
	if !ok {
		t.Fatalf("output: %#v", res.Output)
	}
	apptID, _ := out["appointment_id"].(string)
	if apptID == "" || out["starts_at"] != "2026-10-13T07:40:00Z" {
		t.Fatalf("appointment_id=%q starts_at=%v", apptID, out["starts_at"])
	}
	if out["ends_at"] != "2026-10-13T08:00:00Z" {
		t.Errorf("ends_at = %v, ожидалось 08:00Z (20 мин после старта)", out["ends_at"])
	}
	if out["doctor_id"] != "d-therapist-1" || out["agent_id"] != "booking-test" {
		t.Errorf("output: %v", out)
	}
	auction, _ := out["auction"].(map[string]any)
	if auction["bids"] != 3 || auction["winner_cost"] != 0.41 {
		t.Errorf("auction: %v", auction)
	}

	// Запись в хранилище: слот, данные записи, занятость врача.
	rec, err := st.HGetAll(ctx, ApptKey(apptID))
	if err != nil {
		t.Fatal(err)
	}
	if rec["status"] != "booked" || rec["patient_name"] != "Иванова Анна Петровна" {
		t.Errorf("запись: %v", rec)
	}
	ids, err := st.ZRangeByScore(ctx, BookedKey("d-therapist-1"), -1e18, 1e18, 0)
	if err != nil || len(ids) != 1 || ids[0] != "2026-10-13T07:40:00Z" {
		t.Errorf("занятость врача: %v %v", ids, err)
	}
}

func TestBookRejectsInvalidSlots(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	cases := []struct {
		name    string
		req     BookRequest
		wantErr string
	}{
		{"неизвестный врач", BookRequest{DoctorID: "d-x", SlotStart: "2026-10-13T07:40:00Z"}, "unknown_doctor"},
		{"некорректное время", BookRequest{DoctorID: "d-therapist-1", SlotStart: "вчера"}, "invalid_slot_start"},
		{"в прошлом", bookReq("2026-10-13T06:00:00Z"), "invalid_slot: запись должна начинаться в будущем"},
		{"вне смены (15:00 MSK)", bookReq("2026-10-13T12:00:00Z"), "invalid_slot: запись должна укладываться в смену"},
		{"в выходной (суббота 10:00 MSK)", bookReq("2026-10-17T07:00:00Z"), "invalid_slot: врач не работает"},
		{"не на сетке (10:50 MSK)", bookReq("2026-10-13T07:50:00Z"), "invalid_slot: запись должна начинаться на границе слота"},
		{"дальше 90 суток", bookReq("2027-02-01T08:00:00Z"), "invalid_slot: запись возможна не дальше"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			res := svc.Book(context.Background(), c.req)
			if res.Success {
				t.Fatalf("бронь должна быть отклонена")
			}
			if !strings.Contains(res.Error, c.wantErr) {
				t.Fatalf("ошибка %q, ожидалось %q", res.Error, c.wantErr)
			}
			if res.Retryable {
				t.Errorf("ошибка валидации не повторяется")
			}
		})
	}
}

func TestBookSameSlotTwiceIsSlotTaken(t *testing.T) {
	t.Parallel()
	svc := newSvc(t, testParams(), store.NewMemory())
	ctx := context.Background()
	if res := svc.Book(ctx, bookReq("2026-10-13T07:40:00Z")); !res.Success {
		t.Fatalf("первая бронь: %+v", res)
	}
	res := svc.Book(ctx, bookReq("2026-10-13T07:40:00Z"))
	if res.Success || res.Error != "slot_taken" {
		t.Fatalf("повторная бронь: %+v", res)
	}
	if res.Retryable {
		t.Error("slot_taken не повторяется: аукцион проводится заново")
	}
}

// Одновременные брони одного слота из разных экземпляров: ровно одна проходит (SET NX).
func TestBookConcurrentSingleWinner(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	svc := newSvc(t, testParams(), st)
	const n = 20
	results := make(chan core.Result, n)
	for i := 0; i < n; i++ {
		go func() { results <- svc.Book(context.Background(), bookReq("2026-10-13T07:40:00Z")) }()
	}
	wins := 0
	for i := 0; i < n; i++ {
		if r := <-results; r.Success {
			wins++
		} else if r.Error != "slot_taken" {
			t.Errorf("неожиданная ошибка: %q", r.Error)
		}
	}
	if wins != 1 {
		t.Fatalf("побед %d, ожидалась ровно одна", wins)
	}
}

// failingStore имитирует сбой Redis на выбранной операции.
type failingStore struct {
	*store.Memory
	failSetNX, failHSet, failZAdd, failZRange bool
}

func (f *failingStore) ZRangeByScore(ctx context.Context, k string, lo, hi float64, n int64) ([]string, error) {
	if f.failZRange {
		return nil, errors.New("сеть")
	}
	return f.Memory.ZRangeByScore(ctx, k, lo, hi, n)
}

func (f *failingStore) SetNX(ctx context.Context, k, v string) (bool, error) {
	if f.failSetNX {
		return false, errors.New("сеть")
	}
	return f.Memory.SetNX(ctx, k, v)
}

func (f *failingStore) HSet(ctx context.Context, k string, v map[string]string) error {
	if f.failHSet {
		return errors.New("сеть")
	}
	return f.Memory.HSet(ctx, k, v)
}

func (f *failingStore) ZAdd(ctx context.Context, k string, s float64, m string) error {
	if f.failZAdd {
		return errors.New("сеть")
	}
	return f.Memory.ZAdd(ctx, k, s, m)
}

func TestBookRedisFailuresAreRetryable(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	slot := "2026-10-13T07:40:00Z"

	t.Run("SET NX", func(t *testing.T) {
		t.Parallel()
		svc := newSvc(t, testParams(), &failingStore{Memory: store.NewMemory(), failSetNX: true})
		res := svc.Book(ctx, bookReq(slot))
		if res.Success || !res.Retryable || !strings.HasPrefix(res.Error, "redis:") {
			t.Fatalf("%+v", res)
		}
	})

	t.Run("HSET откатывает слот", func(t *testing.T) {
		t.Parallel()
		fs := &failingStore{Memory: store.NewMemory(), failHSet: true}
		svc := newSvc(t, testParams(), fs)
		res := svc.Book(ctx, bookReq(slot))
		if res.Success || !res.Retryable {
			t.Fatalf("%+v", res)
		}
		// Слот не должен «зависнуть»: после восстановления Redis бронь проходит.
		fs.failHSet = false
		if res := svc.Book(ctx, bookReq(slot)); !res.Success {
			t.Fatalf("после отката слот должен быть свободен: %+v", res)
		}
	})

	t.Run("ZADD откатывает слот и запись", func(t *testing.T) {
		t.Parallel()
		fs := &failingStore{Memory: store.NewMemory(), failZAdd: true}
		svc := newSvc(t, testParams(), fs)
		if res := svc.Book(ctx, bookReq(slot)); res.Success || !res.Retryable {
			t.Fatalf("%+v", res)
		}
		fs.failZAdd = false
		if res := svc.Book(ctx, bookReq(slot)); !res.Success {
			t.Fatalf("после отката слот должен быть свободен: %+v", res)
		}
	})
}

func TestCostAndSlotKeysFormat(t *testing.T) {
	t.Parallel()
	if got := SlotKey("d-1", "2026-10-13T07:40:00Z"); got != "mas:slot:d-1:2026-10-13T07:40:00Z" {
		t.Errorf("SlotKey = %q", got)
	}
	if got := BookedKey("d-1"); got != "mas:doctor:d-1:booked" {
		t.Errorf("BookedKey = %q", got)
	}
	if got := ApptKey("a"); got != "mas:appt:a" {
		t.Errorf("ApptKey = %q", got)
	}
}
