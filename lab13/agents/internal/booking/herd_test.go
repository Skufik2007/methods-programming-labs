package booking

import (
	"context"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

// Находка №12: в CI 21 из 40 одновременных обращений провалились с «slot_taken» после трёх
// раундов аукциона. Все параллельные аукционы видят один и тот же ближайший свободный слот,
// бронь получает только первый, остальные снова сталкиваются на следующем слоте.
// Победитель аукциона — врач; если его слот успели занять, агент должен записать пациента
// на ближайший свободный слот того же врача в пределах окна max_wait_hours.
func TestBookTakenSlotFallsBackToNextFreeSlotOfSameDoctor(t *testing.T) {
	now := time.Date(2026, 10, 13, 6, 0, 0, 0, time.UTC) // вторник, 09:00 по Москве
	loc, _ := time.LoadLocation("Europe/Moscow")
	svc, err := NewService("booking-t", Params{MinLeadMinutes: 60, HorizonDays: 14, Doctors: []DoctorCfg{{
		ID: "d-1", Name: "Врач", Specialty: "neurologist", WorkDays: []int{1, 2, 3, 4, 5},
		WorkStart: "09:00", WorkEnd: "15:00", SlotMinutes: 30,
	}}}, loc, store.NewMemory(), func() time.Time { return now })
	if err != nil {
		t.Fatal(err)
	}
	req := BookRequest{AuctionID: "a", RunID: "r", DoctorID: "d-1", SlotStart: "2026-10-13T07:00:00Z",
		PatientID: "p", PatientName: "П", MaxWaitHours: 72}

	first := svc.Book(context.Background(), req)
	second := svc.Book(context.Background(), req)
	if !first.Success || !second.Success {
		t.Fatalf("обе брони должны пройти: %v / %v", first.Error, second.Error)
	}
	got := second.Output.(map[string]any)["starts_at"]
	if got != "2026-10-13T07:30:00Z" {
		t.Fatalf("вторая бронь — следующий слот того же врача 07:30Z, получено %v", got)
	}

	// Без окна (старые клиенты) поведение прежнее: занятый слот — slot_taken.
	req.MaxWaitHours = 0
	if res := svc.Book(context.Background(), req); res.Success || res.Error != "slot_taken" {
		t.Fatalf("без max_wait_hours ожидался slot_taken, получено %+v", res)
	}
}
