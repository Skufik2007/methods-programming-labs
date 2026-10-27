package bus_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/booking"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/bus"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/registry"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/role"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/stats"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/testutil"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/triage"
)

// Фиксированное «сейчас»: вторник 2026-10-13, 10:00 по Москве.
var fixedNow = time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)

const traceID = "4bf92f3577b34da6a3ce929d0e0e4736"
const traceparent = "00-" + traceID + "-00f067aa0ba902b7-01"

var discardLog = slog.New(slog.NewTextHandler(io.Discard, nil))

func clock() core.Clock { return func() time.Time { return fixedNow } }

func loadRole(t *testing.T, file string) role.Role {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "roles", file))
	if err != nil {
		t.Fatal(err)
	}
	r, err := role.Parse(string(src), registry.Kinds())
	if err != nil {
		t.Fatalf("роль %s: %v", file, err)
	}
	return *r
}

// newAgentBus подключает агента к шине и привязывает его закрытие к тесту.
func newAgentBus(t *testing.T, url string, info bus.Info, counters *stats.Counters) *bus.Bus {
	t.Helper()
	b, err := bus.Connect(context.Background(), url, info, clock(), discardLog, counters)
	if err != nil {
		t.Fatalf("bus.Connect: %v", err)
	}
	t.Cleanup(b.Close)
	return b
}

// runTasks запускает выборку задач шага в фоне; остановка — при очистке теста.
func runTasks(t *testing.T, b *bus.Bus, step string, h core.TaskHandler, concurrency int, ch bus.Chaos) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	cons, err := b.EnsureConsumer(ctx, step)
	if err != nil {
		cancel()
		t.Fatalf("EnsureConsumer: %v", err)
	}
	done := make(chan struct{})
	go func() { b.RunTasks(ctx, cons, h, concurrency, ch); close(done) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
		}
	})
}

func triageHandler(t *testing.T) core.TaskHandler {
	t.Helper()
	r := loadRole(t, "triage.md")
	h, err := triage.New(r.Params)
	if err != nil {
		t.Fatalf("triage.New: %v", err)
	}
	return h
}

func taskMsg(t *testing.T, runID, taskID string, payload any) *nats.Msg {
	t.Helper()
	p, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	task := core.Task{TaskID: taskID, RunID: runID, Step: "triage", Kind: "triage", Attempt: 1,
		CreatedAt: "2026-10-13T07:00:00Z", Payload: p}
	data, err := json.Marshal(task)
	if err != nil {
		t.Fatal(err)
	}
	return &nats.Msg{Subject: "tasks.triage", Data: data, Header: nats.Header{
		"traceparent": []string{traceparent},
		"Nats-Msg-Id": []string{taskID + "-1"},
	}}
}

func triagePayload(complaint string) map[string]any {
	return map[string]any{
		"case":     map[string]any{"complaint": complaint, "age": 34},
		"previous": map[string]any{"specialty": "therapist", "red_flags": []string{}},
	}
}

func publishTask(t *testing.T, js jetstream.JetStream, m *nats.Msg) {
	t.Helper()
	if _, err := js.PublishMsg(context.Background(), m); err != nil {
		t.Fatalf("публикация задачи: %v", err)
	}
}

func newJS(t *testing.T, url string) (*nats.Conn, jetstream.JetStream) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	return nc, js
}

func nextMsg(t *testing.T, sub *nats.Subscription, timeout time.Duration) *nats.Msg {
	t.Helper()
	m, err := sub.NextMsg(timeout)
	if err != nil {
		t.Fatalf("сообщение не пришло за %v: %v", timeout, err)
	}
	return m
}

// Агент триажа: задача из tasks.triage → результат в results.<run_id> с тем же trace_id,
// heartbeat и события received/published/completed.
func TestTriageAgentEndToEnd(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	nc, js := newJS(t, url)

	events, err := nc.SubscribeSync("events.triage-1")
	if err != nil {
		t.Fatal(err)
	}
	hb, err := nc.SubscribeSync(bus.HeartbeatSubject)
	if err != nil {
		t.Fatal(err)
	}
	results, err := nc.SubscribeSync("results.run-1")
	if err != nil {
		t.Fatal(err)
	}
	counters := stats.New(store.NewMemory(), "triage", "triage-1", discardLog)
	b := newAgentBus(t, url, bus.Info{AgentID: "triage-1", Step: "triage", Kind: "triage", Title: "Триаж",
		StartedAt: fixedNow}, counters)
	runTasks(t, b, "triage", triageHandler(t), 2, bus.Chaos{})
	go b.RunHeartbeat(context.Background())

	// heartbeat «ready» сразу при старте
	var hbMsg struct {
		AgentID string `json:"agent_id"`
		Role    string `json:"role"`
		Status  string `json:"status"`
		Lang    string `json:"lang"`
	}
	if err := json.Unmarshal(nextMsg(t, hb, 5*time.Second).Data, &hbMsg); err != nil {
		t.Fatal(err)
	}
	if hbMsg.AgentID != "triage-1" || hbMsg.Role != "triage" || hbMsg.Status != "ready" || hbMsg.Lang != "go" {
		t.Fatalf("heartbeat: %+v", hbMsg)
	}

	publishTask(t, js, taskMsg(t, "run-1", "task-1", triagePayload("Температура 39, кашель третий день")))

	res := nextMsg(t, results, 10*time.Second)
	var r core.Result
	if err := json.Unmarshal(res.Data, &r); err != nil {
		t.Fatal(err)
	}
	if !r.Success || r.TaskID != "task-1" || r.RunID != "run-1" || r.AgentID != "triage-1" || r.Step != "triage" {
		t.Fatalf("результат: %+v", r)
	}
	out, _ := r.Output.(map[string]any)
	// срочное ожидание — из роли triage (max_wait_hours.urgent), а не зашито в тесте
	var triP triage.Params
	triRole := loadRole(t, "triage.md")
	if err := triRole.Params.Decode(&triP); err != nil {
		t.Fatal(err)
	}
	if out["urgency"] != "urgent" || out["max_wait_hours"] != float64(triP.MaxWaitHours["urgent"]) {
		t.Fatalf("output: %v", out)
	}
	// trace_id сохраняется: результат несёт traceparent с тем же trace_id, что у задачи
	tp := res.Header["traceparent"]
	var tpVal string
	if len(tp) > 0 {
		tpVal = tp[0]
	}
	if parts := strings.Split(tpVal, "-"); len(parts) != 4 || parts[1] != traceID {
		t.Fatalf("traceparent результата %q, ожидался trace_id %s", tpVal, traceID)
	}

	// события журнала по задаче
	seen := map[string]bool{}
	for i := 0; i < 10 && !(seen["received"] && seen["published"] && seen["completed"]); i++ {
		var ev core.Event
		if err := json.Unmarshal(nextMsg(t, events, 5*time.Second).Data, &ev); err != nil {
			t.Fatal(err)
		}
		if ev.TraceID != traceID {
			t.Errorf("событие %s без trace_id задачи: %q", ev.Action, ev.TraceID)
		}
		seen[ev.Action] = true
	}
	for _, want := range []string{"received", "published", "completed"} {
		if !seen[want] {
			t.Errorf("нет события %q, получено %v", want, seen)
		}
	}
	if counters.Processed() != 1 || counters.Failed() != 0 {
		t.Errorf("счётчики: processed=%d failed=%d", counters.Processed(), counters.Failed())
	}
}

// Некорректные сообщения (битый JSON, пустой run_id) не роняют агента: их отбрасывают,
// а следующая корректная задача обрабатывается.
func TestInvalidTaskJSONDoesNotCrashAgent(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	nc, js := newJS(t, url)

	results, err := nc.SubscribeSync("results.>")
	if err != nil {
		t.Fatal(err)
	}
	events, err := nc.SubscribeSync("events.triage-bad")
	if err != nil {
		t.Fatal(err)
	}
	counters := stats.New(store.NewMemory(), "triage", "triage-bad", discardLog)
	b := newAgentBus(t, url, bus.Info{AgentID: "triage-bad", Step: "triage", Kind: "triage"}, counters)
	runTasks(t, b, "triage", triageHandler(t), 1, bus.Chaos{})

	// битый JSON и задача без run_id
	if _, err := js.PublishMsg(context.Background(), &nats.Msg{Subject: "tasks.triage", Data: []byte("{не json")}); err != nil {
		t.Fatal(err)
	}
	noRun, _ := json.Marshal(core.Task{TaskID: "no-run", Step: "triage", Payload: json.RawMessage(`{}`)})
	if _, err := js.PublishMsg(context.Background(), &nats.Msg{Subject: "tasks.triage", Data: noRun}); err != nil {
		t.Fatal(err)
	}
	// корректная задача после них
	publishTask(t, js, taskMsg(t, "run-ok", "task-ok", triagePayload("Насморк, слабость")))

	res := nextMsg(t, results, 10*time.Second)
	if res.Subject != "results.run-ok" {
		t.Fatalf("первый результат должен быть от корректной задачи, а не от %s", res.Subject)
	}
	var r core.Result
	_ = json.Unmarshal(res.Data, &r)
	if !r.Success {
		t.Fatalf("корректная задача не обработана: %+v", r)
	}

	// плохие сообщения попали в журнал как failed
	failed := 0
	for i := 0; i < 5; i++ {
		m, err := events.NextMsg(2 * time.Second)
		if err != nil {
			break
		}
		var ev core.Event
		_ = json.Unmarshal(m.Data, &ev)
		if ev.Action == "failed" {
			failed++
		}
	}
	if failed < 2 {
		t.Errorf("ожидалось минимум 2 события failed для плохих сообщений, получено %d", failed)
	}
	if counters.Failed() < 2 {
		t.Errorf("счётчик failed = %d, ожидалось не меньше 2", counters.Failed())
	}
	// неподтверждённые/повторные сообщения не порождают лишних результатов
	if extra, err := results.NextMsg(300 * time.Millisecond); err == nil {
		t.Errorf("лишний результат от плохих сообщений: %s", extra.Subject)
	}
}

// Два экземпляра на одном консьюмере делят задачи: каждый получает хотя бы одну из 20.
func TestTwoInstancesShareTheQueue(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	nc, js := newJS(t, url)

	results, err := nc.SubscribeSync("results.>")
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"triage-1", "triage-2"} {
		c := stats.New(store.NewMemory(), "triage", id, discardLog)
		b := newAgentBus(t, url, bus.Info{AgentID: id, Step: "triage", Kind: "triage"}, c)
		// небольшая задержка делает распределение заметным
		runTasks(t, b, "triage", triageHandler(t), 1, bus.Chaos{Slow: 30 * time.Millisecond})
	}

	const n = 20
	for i := 0; i < n; i++ {
		publishTask(t, js, taskMsg(t, fmt.Sprintf("run-%02d", i), fmt.Sprintf("task-%02d", i),
			triagePayload("Кашель, температура")))
	}

	byAgent := map[string]int{}
	seenTasks := map[string]bool{}
	deadline := time.Now().Add(30 * time.Second)
	for len(seenTasks) < n && time.Now().Before(deadline) {
		m, err := results.NextMsg(time.Until(deadline))
		if err != nil {
			break
		}
		var r core.Result
		if err := json.Unmarshal(m.Data, &r); err != nil {
			t.Fatal(err)
		}
		if seenTasks[r.TaskID] {
			t.Errorf("задача %s обработана дважды", r.TaskID)
		}
		seenTasks[r.TaskID] = true
		byAgent[r.AgentID]++
	}
	if len(seenTasks) != n {
		t.Fatalf("обработано %d из %d задач: %v", len(seenTasks), n, byAgent)
	}
	if byAgent["triage-1"] == 0 || byAgent["triage-2"] == 0 {
		t.Fatalf("задачи не распределились между экземплярами: %v", byAgent)
	}
}

// Статус stopping уходит в heartbeat (при остановке агент шлёт его перед закрытием).
func TestStoppingHeartbeat(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	nc, _ := newJS(t, url)
	hb, err := nc.SubscribeSync(bus.HeartbeatSubject)
	if err != nil {
		t.Fatal(err)
	}
	b := newAgentBus(t, url, bus.Info{AgentID: "triage-s", Step: "triage", Kind: "triage"},
		stats.New(store.NewMemory(), "triage", "triage-s", discardLog))
	b.SendHeartbeat("stopping")
	var hbMsg struct {
		Status string `json:"status"`
	}
	if err := json.Unmarshal(nextMsg(t, hb, 5*time.Second).Data, &hbMsg); err != nil || hbMsg.Status != "stopping" {
		t.Fatalf("heartbeat: %+v %v", hbMsg, err)
	}
}

// Аукцион записи: два агента записи отвечают ставками на auction.booking; победитель
// бронирует слот через agent.<id>.book; повторная бронь того же слота — slot_taken.
// Хранилище — miniredis (одно на оба агента, как общий Redis в compose).
func TestBookingAuctionAndReservation(t *testing.T) {
	t.Parallel()
	url := testutil.StartNATS(t)
	mr := miniredis.RunT(t)
	rs, err := store.NewRedis("redis://" + mr.Addr() + "/0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = rs.Close() })

	nc, _ := newJS(t, url)
	loc := mustMoscow(t)

	startBooking := func(agentID, roleFile string) {
		r := loadRole(t, roleFile)
		b := newAgentBus(t, url, bus.Info{AgentID: agentID, Step: "booking", Kind: "booking"},
			stats.New(rs, "booking", agentID, discardLog))
		inst, err := booking.New(core.Deps{AgentID: agentID, Step: "booking", Store: rs, Clock: clock(),
			Loc: loc, Sink: b, Log: discardLog}, r.Params)
		if err != nil {
			t.Fatalf("booking.New(%s): %v", agentID, err)
		}
		for _, s := range inst.Subscriptions {
			if _, err := b.Subscribe(s); err != nil {
				t.Fatalf("подписка %s: %v", s.Subject, err)
			}
		}
	}
	startBooking("booking-a-1", "booking-a.md")
	startBooking("booking-b-1", "booking-b.md")

	// Аукцион: запрос с ответами-ставками (inbox), окно сбора 500 мс.
	collect := func(req booking.AuctionRequest) []booking.Bid {
		t.Helper()
		data, _ := json.Marshal(req)
		inbox := nc.NewRespInbox()
		sub, err := nc.SubscribeSync(inbox)
		if err != nil {
			t.Fatal(err)
		}
		defer sub.Unsubscribe()
		if err := nc.PublishRequest("auction.booking", inbox, data); err != nil {
			t.Fatal(err)
		}
		var bids []booking.Bid
		deadline := time.Now().Add(500 * time.Millisecond)
		for {
			m, err := sub.NextMsg(time.Until(deadline))
			if err != nil {
				break
			}
			var bid booking.Bid
			if err := json.Unmarshal(m.Data, &bid); err != nil {
				t.Fatal(err)
			}
			bids = append(bids, bid)
		}
		return bids
	}

	req := booking.AuctionRequest{AuctionID: "auction-1", RunID: "run-b1", Specialty: "therapist",
		Urgency: "routine", MaxWaitHours: 72, Now: fixedNow.Format(time.RFC3339)}
	bids := collect(req)
	if len(bids) != 2 {
		t.Fatalf("ставок %d, ожидалось 2 (по одной от каждого агента): %+v", len(bids), bids)
	}
	// Каждая ставка сходится с формулой раздела 6.
	for _, bid := range bids {
		if !bid.Eligible {
			t.Errorf("ставка %s не eligible: %s", bid.AgentID, bid.Reason)
		}
		want := booking.Cost(bid.Availability, bid.Load, bid.Skill)
		if diff := bid.Cost - want; diff > 1e-9 || diff < -1e-9 {
			t.Errorf("cost %s = %v, по формуле %v", bid.AgentID, bid.Cost, want)
		}
	}
	winner := bids[0]
	for _, bid := range bids[1:] {
		if bid.Cost < winner.Cost || (bid.Cost == winner.Cost && (bid.SlotStart < winner.SlotStart ||
			(bid.SlotStart == winner.SlotStart && bid.AgentID < winner.AgentID))) {
			winner = bid
		}
	}

	// Исключённый агент не отвечает.
	excluded := req
	excluded.AuctionID = "auction-2"
	excluded.ExcludeAgents = []string{winner.AgentID}
	if rest := collect(excluded); len(rest) != 1 || rest[0].AgentID == winner.AgentID {
		t.Fatalf("после exclude_agents ответил один другой агент: %+v", rest)
	}

	// Бронь у победителя.
	book := func(auctionID string) map[string]any {
		t.Helper()
		data, _ := json.Marshal(booking.BookRequest{AuctionID: auctionID, RunID: "run-b1",
			DoctorID: winner.DoctorID, SlotStart: winner.SlotStart, PatientID: "p-7", PatientName: "Петров",
			Bids: len(bids), WinnerCost: winner.Cost})
		m, err := nc.Request("agent."+winner.AgentID+".book", data, 5*time.Second)
		if err != nil {
			t.Fatalf("запрос брони: %v", err)
		}
		var res core.Result
		if err := json.Unmarshal(m.Data, &res); err != nil {
			t.Fatal(err)
		}
		out, _ := res.Output.(map[string]any)
		out["_success"] = res.Success
		out["_error"] = res.Error
		return out
	}
	first := book("auction-1")
	if first["_success"] != true || first["appointment_id"] == "" {
		t.Fatalf("бронь: %v", first)
	}
	if first["starts_at"] != winner.SlotStart {
		t.Errorf("starts_at = %v, ожидалось %s", first["starts_at"], winner.SlotStart)
	}
	second := book("auction-3")
	if second["_success"] == true || second["_error"] != "slot_taken" {
		t.Fatalf("повторная бронь того же слота: %v", second)
	}
}

func mustMoscow(t *testing.T) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation("Europe/Moscow")
	if err != nil {
		t.Fatal(err)
	}
	return loc
}
