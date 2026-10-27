package booking

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

// recSink запоминает события и публикации вместо NATS.
type recSink struct {
	mu     sync.Mutex
	events []core.Event
}

func (r *recSink) Emit(_ context.Context, e core.Event) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, e)
}

func (r *recSink) Publish(context.Context, string, any) error { return nil }

func (r *recSink) actions() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, 0, len(r.events))
	for _, e := range r.events {
		out = append(out, e.Action)
	}
	return out
}

func adapterFor(t *testing.T, st store.Store, sink core.Sink) (*adapter, *Service) {
	t.Helper()
	svc := newSvc(t, testParams(), st)
	return &adapter{svc: svc, sink: sink, log: slog.New(slog.NewTextHandler(io.Discard, nil))}, svc
}

func TestNewBuildsTwoSubscriptions(t *testing.T) {
	t.Parallel()
	var params yaml.Node
	src := "min_lead_minutes: 30\nhorizon_days: 14\ndoctors:\n" +
		"  - id: d-therapist-1\n    specialty: therapist\n    work_days: [1, 2, 3, 4, 5]\n" +
		"    work_start: \"08:00\"\n    work_end: \"14:00\"\n    slot_minutes: 20\n"
	if err := yaml.Unmarshal([]byte(src), &params); err != nil {
		t.Fatal(err)
	}
	inst, err := New(core.Deps{
		AgentID: "booking-7", Step: "booking", Store: store.NewMemory(), Loc: moscowLoc(t),
		Clock: fixedClock(), Sink: &recSink{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, *params.Content[0])
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if inst.Tasks != nil {
		t.Error("booking не читает поток TASKS")
	}
	subjects := make([]string, 0, len(inst.Subscriptions))
	for _, s := range inst.Subscriptions {
		subjects = append(subjects, s.Subject)
	}
	if strings.Join(subjects, ",") != "auction.booking,agent.booking-7.book" {
		t.Fatalf("subjects = %v", subjects)
	}
}

func TestNewPropagatesParamErrors(t *testing.T) {
	t.Parallel()
	var params yaml.Node
	if err := yaml.Unmarshal([]byte("doctors: [1, 2]\n"), &params); err != nil {
		t.Fatal(err)
	}
	if _, err := New(core.Deps{AgentID: "b", Store: store.NewMemory(), Loc: moscowLoc(t),
		Clock: fixedClock(), Sink: &recSink{}, Log: slog.Default()}, *params.Content[0]); err == nil {
		t.Fatal("неверный тип doctors должен давать ошибку")
	}
}

func TestAuctionReplyAndEvent(t *testing.T) {
	t.Parallel()
	sink := &recSink{}
	a, _ := adapterFor(t, store.NewMemory(), sink)
	data, _ := json.Marshal(AuctionRequest{AuctionID: "a-1", RunID: "r-1", Specialty: "therapist",
		Urgency: "routine", MaxWaitHours: 72, Now: now.Format(time.RFC3339)})

	reply, failed := a.auction(context.Background(), data)
	if failed || reply == nil {
		t.Fatalf("ожидалась ставка: failed=%v reply=%s", failed, reply)
	}
	var bid Bid
	if err := json.Unmarshal(reply, &bid); err != nil {
		t.Fatal(err)
	}
	if bid.AuctionID != "a-1" || bid.AgentID != "booking-test" || !bid.Eligible {
		t.Fatalf("ставка: %+v", bid)
	}
	if got := sink.actions(); len(got) != 1 || got[0] != "bid" {
		t.Fatalf("события: %v", got)
	}
}

func TestAuctionIneligibleEventSummary(t *testing.T) {
	t.Parallel()
	sink := &recSink{}
	a, _ := adapterFor(t, store.NewMemory(), sink)
	data, _ := json.Marshal(AuctionRequest{AuctionID: "a-2", Specialty: "cardiologist", Urgency: "urgent",
		MaxWaitHours: 4, Now: now.Format(time.RFC3339)})
	reply, failed := a.auction(context.Background(), data)
	if failed || reply == nil {
		t.Fatalf("ставка «не участвует» тоже отвечает: failed=%v", failed)
	}
	sink.mu.Lock()
	defer sink.mu.Unlock()
	if !strings.Contains(sink.events[0].Summary, "не участвует: no_matching_doctor") {
		t.Errorf("summary: %q", sink.events[0].Summary)
	}
}

func TestAuctionExcludedAgentDoesNotAnswer(t *testing.T) {
	t.Parallel()
	a, _ := adapterFor(t, store.NewMemory(), &recSink{})
	data, _ := json.Marshal(AuctionRequest{AuctionID: "a-3", Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, ExcludeAgents: []string{"booking-other", "booking-test"}, Now: now.Format(time.RFC3339)})
	reply, failed := a.auction(context.Background(), data)
	if reply != nil || failed {
		t.Fatalf("исключённый агент не отвечает и не считается сбоем: reply=%s failed=%v", reply, failed)
	}
}

func TestAuctionBadRequestAndInternalError(t *testing.T) {
	t.Parallel()
	a, _ := adapterFor(t, store.NewMemory(), &recSink{})
	if reply, failed := a.auction(context.Background(), []byte("не json")); reply != nil || !failed {
		t.Errorf("битый JSON: reply=%s failed=%v", reply, failed)
	}
	if reply, failed := a.auction(context.Background(), []byte(`{"specialty":"therapist"}`)); reply != nil || !failed {
		t.Errorf("без auction_id: reply=%s failed=%v", reply, failed)
	}

	// Сбой Redis при расчёте ставки: ответа нет, задача считается неуспешной и повторяемой.
	fs := &failingStore{Memory: store.NewMemory(), failZRange: true}
	a2, _ := adapterFor(t, fs, &recSink{})
	data, _ := json.Marshal(AuctionRequest{AuctionID: "a-4", Specialty: "therapist", Urgency: "routine",
		MaxWaitHours: 72, Now: now.Format(time.RFC3339)})
	if reply, failed := a2.auction(context.Background(), data); reply != nil || !failed {
		t.Errorf("сбой Redis: reply=%s failed=%v", reply, failed)
	}
}

func TestBookAdapterSuccessAndEvents(t *testing.T) {
	t.Parallel()
	sink := &recSink{}
	a, _ := adapterFor(t, store.NewMemory(), sink)
	data, _ := json.Marshal(BookRequest{AuctionID: "a-1", RunID: "r-1", DoctorID: "d-therapist-1",
		SlotStart: "2026-10-13T07:40:00Z", PatientID: "p-1", PatientName: "Иванова"})

	reply, failed := a.book(context.Background(), data)
	if failed {
		t.Fatalf("успешная бронь не должна считаться сбоем")
	}
	var res core.Result
	if err := json.Unmarshal(reply, &res); err != nil {
		t.Fatal(err)
	}
	if !res.Success || res.Step != "booking" {
		t.Fatalf("результат: %+v", res)
	}
	if got := strings.Join(sink.actions(), ","); got != "won,booked" {
		t.Fatalf("события: %s", got)
	}
}

func TestBookAdapterSlotTakenIsNotFailure(t *testing.T) {
	t.Parallel()
	sink := &recSink{}
	a, _ := adapterFor(t, store.NewMemory(), sink)
	data, _ := json.Marshal(BookRequest{AuctionID: "a-1", RunID: "r-1", DoctorID: "d-therapist-1",
		SlotStart: "2026-10-13T07:40:00Z"})
	if _, failed := a.book(context.Background(), data); failed {
		t.Fatal("первая бронь не сбой")
	}
	reply, failed := a.book(context.Background(), data)
	if failed {
		t.Fatal("slot_taken — штатная гонка, а не сбой (nak не нужен)")
	}
	var res core.Result
	_ = json.Unmarshal(reply, &res)
	if res.Success || res.Error != "slot_taken" {
		t.Fatalf("результат: %+v", res)
	}
	if got := strings.Join(sink.actions(), ","); !strings.HasSuffix(got, "failed") {
		t.Errorf("ожидалось событие failed: %s", got)
	}
}

func TestBookAdapterRetryableFailure(t *testing.T) {
	t.Parallel()
	fs := &failingStore{Memory: store.NewMemory(), failSetNX: true}
	a, _ := adapterFor(t, fs, &recSink{})
	data, _ := json.Marshal(BookRequest{DoctorID: "d-therapist-1", SlotStart: "2026-10-13T07:40:00Z"})
	reply, failed := a.book(context.Background(), data)
	if !failed {
		t.Fatal("сбой Redis должен давать failed=true (повтор задачи)")
	}
	var res core.Result
	_ = json.Unmarshal(reply, &res)
	if !res.Retryable {
		t.Errorf("результат должен быть помечен retryable: %+v", res)
	}
}

func TestBookAdapterBadJSON(t *testing.T) {
	t.Parallel()
	a, _ := adapterFor(t, store.NewMemory(), &recSink{})
	reply, failed := a.book(context.Background(), []byte("{"))
	if !failed {
		t.Fatal("битый запрос брони — сбой")
	}
	var res core.Result
	if err := json.Unmarshal(reply, &res); err != nil {
		t.Fatal(err)
	}
	if res.Error != "invalid_request" {
		t.Errorf("error = %q", res.Error)
	}
}
