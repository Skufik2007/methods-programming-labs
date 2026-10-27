// Пакет booking — запись к врачу: ставки в аукционе (раздел 6) и атомарная бронь.
// Бизнес-логика зависит только от store.Store и часов, NATS здесь нет.
package booking

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/google/uuid"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/directory"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/schedule"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

const loadDays = 7

type DoctorCfg struct {
	ID          string `yaml:"id"`
	Name        string `yaml:"name"`
	Specialty   string `yaml:"specialty"`
	WorkDays    []int  `yaml:"work_days"`
	WorkStart   string `yaml:"work_start"`
	WorkEnd     string `yaml:"work_end"`
	SlotMinutes int    `yaml:"slot_minutes"`
}

type Doctor struct {
	ID, Name, Specialty string
	Sched               schedule.Schedule
}

// AuctionRequest — запрос на auction.booking.
type AuctionRequest struct {
	AuctionID     string   `json:"auction_id"`
	RunID         string   `json:"run_id"`
	Specialty     string   `json:"specialty"`
	Urgency       string   `json:"urgency"`
	MaxWaitHours  float64  `json:"max_wait_hours"`
	Now           string   `json:"now"`
	ExcludeAgents []string `json:"exclude_agents"`
}

// Bid — ставка агента.
type Bid struct {
	AuctionID    string  `json:"auction_id"`
	AgentID      string  `json:"agent_id"`
	DoctorID     string  `json:"doctor_id"`
	SlotStart    string  `json:"slot_start"`
	Skill        float64 `json:"skill"`
	Availability float64 `json:"availability"`
	Load         float64 `json:"load"`
	Cost         float64 `json:"cost"`
	Eligible     bool    `json:"eligible"`
	Reason       string  `json:"reason"`
}

// BookRequest — запрос на agent.<id>.book. Bids и WinnerCost — необязательные сведения
// оркестратора для поля output.auction.
type BookRequest struct {
	AuctionID   string  `json:"auction_id"`
	RunID       string  `json:"run_id"`
	DoctorID    string  `json:"doctor_id"`
	SlotStart   string  `json:"slot_start"`
	PatientID   string  `json:"patient_id"`
	PatientName string  `json:"patient_name"`
	Bids        int     `json:"bids"`
	WinnerCost  float64 `json:"winner_cost"`
	// MaxWaitHours — окно ожидания пациента. Если выигранный слот уже занят, агент записывает
	// на ближайший свободный слот того же врача в этом окне; 0 — без замены (slot_taken).
	MaxWaitHours float64 `json:"max_wait_hours"`
}

type Service struct {
	agentID string
	doctors []Doctor
	lead    time.Duration
	horizon int
	loc     *time.Location
	st      store.Store
	clock   core.Clock
}

func NewService(agentID string, p Params, loc *time.Location, st store.Store, clock core.Clock) (*Service, error) {
	if len(p.Doctors) == 0 {
		return nil, fmt.Errorf("в параметрах нет ни одного врача (doctors)")
	}
	if p.MinLeadMinutes < 0 {
		return nil, fmt.Errorf("min_lead_minutes не может быть отрицательным")
	}
	if p.HorizonDays == 0 {
		p.HorizonDays = 14
	}
	if p.HorizonDays < 1 || p.HorizonDays > schedule.MaxBookingDaysAhead {
		return nil, fmt.Errorf("horizon_days должен быть от 1 до %d", schedule.MaxBookingDaysAhead)
	}
	seen := map[string]bool{}
	s := &Service{agentID: agentID, lead: time.Duration(p.MinLeadMinutes) * time.Minute,
		horizon: p.HorizonDays, loc: loc, st: st, clock: clock}
	for _, d := range p.Doctors {
		if d.ID == "" || seen[d.ID] {
			return nil, fmt.Errorf("врач: пустой или повторяющийся id %q", d.ID)
		}
		seen[d.ID] = true
		if !directory.Valid(d.Specialty) {
			return nil, fmt.Errorf("врач %s: специальность %q не из справочника", d.ID, d.Specialty)
		}
		sc, err := schedule.New(d.WorkDays, d.WorkStart, d.WorkEnd, d.SlotMinutes)
		if err != nil {
			return nil, fmt.Errorf("врач %s: %w", d.ID, err)
		}
		s.doctors = append(s.doctors, Doctor{ID: d.ID, Name: d.Name, Specialty: d.Specialty, Sched: sc})
	}
	return s, nil
}

// Skill — раздел 6, п. 2.
func Skill(doctorSpecialty, requested, urgency string) float64 {
	switch {
	case doctorSpecialty == requested:
		return 1.0
	case doctorSpecialty == "therapist" && urgency != "urgent":
		return 0.5
	}
	return 0
}

// Cost — формула контракта; меньше — лучше.
func Cost(availability, load, skill float64) float64 {
	return 0.5*(1-availability) + 0.3*load + 0.2*(1-skill)
}

func BookedKey(doctorID string) string { return "mas:doctor:" + doctorID + ":booked" }
func SlotKey(doctorID, slotStart string) string {
	return "mas:slot:" + doctorID + ":" + slotStart
}
func ApptKey(id string) string { return "mas:appt:" + id }

func unix(t time.Time) float64 { return float64(t.Unix()) }

// busy читает занятые слоты врача в окне [from, to]; длина занятого интервала — шаг сетки.
func (s *Service) busy(ctx context.Context, d Doctor, from, to time.Time) ([]schedule.Interval, error) {
	members, err := s.st.ZRangeByScore(ctx, BookedKey(d.ID), unix(from), unix(to), 0)
	if err != nil {
		return nil, err
	}
	step := time.Duration(d.Sched.SlotMinutes) * time.Minute
	out := make([]schedule.Interval, 0, len(members))
	for _, m := range members {
		t, err := time.Parse(time.RFC3339, m)
		if err != nil {
			continue // мусор в zset не должен ломать аукцион
		}
		out = append(out, schedule.Interval{Start: t.UTC(), End: t.UTC().Add(step)})
	}
	return out, nil
}

// firstFree — ближайший свободный слот не раньше now+lead в пределах горизонта.
func (s *Service) firstFree(ctx context.Context, d Doctor, now time.Time) (schedule.Slot, bool, error) {
	// -1 нс: слот ровно через min_lead минут допустим, а FreeSlots требует «строго после».
	from := now.Add(s.lead).Add(-time.Nanosecond)
	to := from.AddDate(0, 0, s.horizon+1)
	busy, err := s.busy(ctx, d, from.Add(-24*time.Hour), to)
	if err != nil {
		return schedule.Slot{}, false, err
	}
	for i := 0; i <= s.horizon; i++ {
		day := from.AddDate(0, 0, i)
		if free := schedule.FreeSlots(d.Sched, day, s.loc, busy, from, s.horizon+1); len(free) > 0 {
			return free[0], true, nil
		}
	}
	return schedule.Slot{}, false, nil
}

// claimNext атомарно занимает ближайший свободный слот врача строго после after и не позже
// deadline. Нужен при одновременных аукционах: все они видят один и тот же ближайший слот,
// и без замены проигравшие бронь сталкивались бы раунд за раундом (находка №12).
func (s *Service) claimNext(ctx context.Context, d Doctor, after, deadline time.Time, apptID string) (time.Time, bool, error) {
	busy, err := s.busy(ctx, d, after.Add(-24*time.Hour), deadline.Add(24*time.Hour))
	if err != nil {
		return time.Time{}, false, err
	}
	for day := after; !day.After(deadline.Add(24 * time.Hour)); day = day.AddDate(0, 0, 1) {
		for _, slot := range schedule.FreeSlots(d.Sched, day, s.loc, busy, after, s.horizon+1) {
			if slot.Start.After(deadline) {
				return time.Time{}, false, nil
			}
			key := SlotKey(d.ID, slot.Start.UTC().Format(time.RFC3339))
			ok, err := s.st.SetNX(ctx, key, apptID)
			if err != nil {
				return time.Time{}, false, err
			}
			if ok {
				return slot.Start.UTC(), true, nil
			}
		}
	}
	return time.Time{}, false, nil
}

// load — доля занятых слотов врача на ближайшие 7 суток.
func (s *Service) load(ctx context.Context, d Doctor, now time.Time) (float64, error) {
	end := now.AddDate(0, 0, loadDays)
	capacity := 0
	for i := 0; i <= loadDays; i++ {
		for _, sl := range schedule.DaySlots(d.Sched, now.AddDate(0, 0, i), s.loc) {
			if sl.Start.After(now) && !sl.Start.After(end) {
				capacity++
			}
		}
	}
	if capacity == 0 {
		return 1, nil
	}
	booked, err := s.st.ZRangeByScore(ctx, BookedKey(d.ID), unix(now), unix(end), 0)
	if err != nil {
		return 0, err
	}
	l := float64(len(booked)) / float64(capacity)
	if l > 1 {
		l = 1
	}
	return l, nil
}

// Bid строит ставку по лучшему врачу агента. Если подходящих нет, возвращает
// ставку eligible=false с причиной — оркестратору полезно знать, почему агент отказался.
func (s *Service) Bid(ctx context.Context, req AuctionRequest) (Bid, error) {
	now := s.clock.Now()
	if t, err := time.Parse(time.RFC3339, req.Now); err == nil {
		now = t.UTC()
	}
	var (
		best     *Bid
		fallback = Bid{AuctionID: req.AuctionID, AgentID: s.agentID, Reason: "no_matching_doctor"}
		bestSkil = -1.0
	)
	for _, d := range s.doctors {
		skill := Skill(d.Specialty, req.Specialty, req.Urgency)
		if skill == 0 {
			continue
		}
		bid := Bid{AuctionID: req.AuctionID, AgentID: s.agentID, DoctorID: d.ID, Skill: skill}
		slot, ok, err := s.firstFree(ctx, d, now)
		if err != nil {
			return Bid{}, err
		}
		if !ok {
			bid.Reason = "no_free_slots"
		} else {
			bid.SlotStart = slot.Start.Format(time.RFC3339)
			waitH := slot.Start.Sub(now).Hours()
			if waitH < 0 {
				waitH = 0
			}
			switch {
			case waitH > req.MaxWaitHours:
				bid.Reason = "wait_exceeds_max"
			default:
				if req.MaxWaitHours > 0 {
					bid.Availability = 1 - waitH/req.MaxWaitHours
				}
				if bid.Load, err = s.load(ctx, d, now); err != nil {
					return Bid{}, err
				}
				bid.Cost = Cost(bid.Availability, bid.Load, skill)
				bid.Eligible = true
			}
		}
		if bid.Eligible {
			if best == nil || better(bid, *best) {
				b := bid
				best = &b
			}
		} else if skill > bestSkil {
			bestSkil, fallback = skill, bid
		}
	}
	if best != nil {
		return *best, nil
	}
	return fallback, nil
}

// better: меньший cost, при равенстве — раньше слот, затем doctor_id (внутри агента).
func better(a, b Bid) bool {
	if a.Cost != b.Cost {
		return a.Cost < b.Cost
	}
	if a.SlotStart != b.SlotStart {
		return a.SlotStart < b.SlotStart
	}
	return a.DoctorID < b.DoctorID
}

func (s *Service) doctor(id string) (Doctor, bool) {
	for _, d := range s.doctors {
		if d.ID == id {
			return d, true
		}
	}
	return Doctor{}, false
}

// Book бронирует слот атомарно (SET NX). Результат — в формате 4.2.
func (s *Service) Book(ctx context.Context, req BookRequest) core.Result {
	res := core.Result{
		TaskID: req.AuctionID, RunID: req.RunID, Step: "booking", AgentID: s.agentID,
		Output: map[string]any{},
	}
	started := s.clock.Now()
	fail := func(msg string, retryable bool) core.Result {
		res.Success, res.Error, res.Retryable = false, msg, retryable
		res.DurationMS = s.clock.Now().Sub(started).Milliseconds()
		res.FinishedAt = s.clock.Now().Format(time.RFC3339)
		return res
	}

	d, ok := s.doctor(req.DoctorID)
	if !ok {
		return fail("unknown_doctor", false)
	}
	start, err := time.Parse(time.RFC3339, req.SlotStart)
	if err != nil {
		return fail("invalid_slot_start", false)
	}
	start = start.UTC()
	end := start.Add(time.Duration(d.Sched.SlotMinutes) * time.Minute)
	if err := schedule.ValidateBookingWindow(start, s.clock.Now(), schedule.MaxBookingDaysAhead); err != nil {
		return fail("invalid_slot: "+err.Error(), false)
	}
	if err := schedule.ValidateWithinShift(start, end, d.Sched, s.loc); err != nil {
		return fail("invalid_slot: "+err.Error(), false)
	}
	if err := schedule.ValidateSlotGrid(start, end, d.Sched, s.loc); err != nil {
		return fail("invalid_slot: "+err.Error(), false)
	}

	slotStart := start.Format(time.RFC3339)
	apptID := uuid.NewString()
	taken, err := s.st.SetNX(ctx, SlotKey(d.ID, slotStart), apptID)
	if err != nil {
		return fail("redis: "+err.Error(), true)
	}
	if !taken {
		if req.MaxWaitHours <= 0 {
			return fail("slot_taken", false)
		}
		deadline := s.clock.Now().Add(time.Duration(req.MaxWaitHours * float64(time.Hour)))
		alt, ok, err := s.claimNext(ctx, d, start, deadline, apptID)
		if err != nil {
			return fail("redis: "+err.Error(), true)
		}
		if !ok {
			return fail("slot_taken", false)
		}
		start, slotStart = alt, alt.Format(time.RFC3339)
		end = start.Add(time.Duration(d.Sched.SlotMinutes) * time.Minute)
	}
	rec := map[string]string{
		"appointment_id": apptID, "run_id": req.RunID, "auction_id": req.AuctionID,
		"doctor_id": d.ID, "doctor_name": d.Name, "specialty": d.Specialty,
		"patient_id": req.PatientID, "patient_name": req.PatientName,
		"starts_at": slotStart, "ends_at": end.Format(time.RFC3339),
		"agent_id": s.agentID, "status": "booked", "created_at": s.clock.Now().Format(time.RFC3339),
	}
	if err := s.st.HSet(ctx, ApptKey(apptID), rec); err != nil {
		_ = s.st.Del(ctx, SlotKey(d.ID, slotStart)) // откат, чтобы слот не «завис» без записи
		return fail("redis: "+err.Error(), true)
	}
	if err := s.st.ZAdd(ctx, BookedKey(d.ID), unix(start), slotStart); err != nil {
		_ = s.st.Del(ctx, SlotKey(d.ID, slotStart), ApptKey(apptID))
		return fail("redis: "+err.Error(), true)
	}

	res.Success = true
	res.Output = map[string]any{
		"appointment_id": apptID, "doctor_id": d.ID, "doctor_name": d.Name, "specialty": d.Specialty,
		"starts_at": slotStart, "ends_at": rec["ends_at"], "agent_id": s.agentID,
		"auction": map[string]any{"bids": req.Bids, "winner_cost": req.WinnerCost},
	}
	res.DurationMS = s.clock.Now().Sub(started).Milliseconds()
	res.FinishedAt = s.clock.Now().Format(time.RFC3339)
	return res
}

// Doctors возвращает идентификаторы врачей агента (для журнала).
func (s *Service) Doctors() []string {
	ids := make([]string, len(s.doctors))
	for i, d := range s.doctors {
		ids[i] = d.ID
	}
	sort.Strings(ids)
	return ids
}
