package booking

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
)

// Params — раздел параметров роли booking.
type Params struct {
	MinLeadMinutes int         `yaml:"min_lead_minutes"`
	HorizonDays    int         `yaml:"horizon_days"`
	Doctors        []DoctorCfg `yaml:"doctors"`
}

// New собирает экземпляр вида booking: две подписки core NATS, поток TASKS не читается.
func New(d core.Deps, params yaml.Node) (core.Instance, error) {
	var p Params
	if err := params.Decode(&p); err != nil {
		return core.Instance{}, fmt.Errorf("параметры booking: %w", err)
	}
	svc, err := NewService(d.AgentID, p, d.Loc, d.Store, d.Clock)
	if err != nil {
		return core.Instance{}, fmt.Errorf("параметры booking: %w", err)
	}
	a := &adapter{svc: svc, sink: d.Sink, log: d.Log}
	return core.Instance{Subscriptions: []core.Subscription{
		{Subject: "auction.booking", Span: "auction.bid", Handle: a.auction},
		{Subject: "agent." + d.AgentID + ".book", Span: "booking.book", Handle: a.book},
	}}, nil
}

type adapter struct {
	svc  *Service
	sink core.Sink
	log  *slog.Logger
}

func (a *adapter) auction(ctx context.Context, data []byte) ([]byte, bool) {
	var req AuctionRequest
	if err := json.Unmarshal(data, &req); err != nil || req.AuctionID == "" {
		a.log.Warn("некорректный запрос аукциона", "err", err)
		return nil, true
	}
	for _, id := range req.ExcludeAgents {
		if id == a.svc.agentID {
			return nil, false // нас исключили (например, после slot_taken)
		}
	}
	bid, err := a.svc.Bid(ctx, req)
	if err != nil {
		a.log.Error("ставка не построена", "auction_id", req.AuctionID, "err", err)
		return nil, true
	}
	trace.SpanFromContext(ctx).SetAttributes(attribute.String("auction.id", req.AuctionID),
		attribute.Float64("bid.cost", bid.Cost), attribute.Bool("bid.eligible", bid.Eligible))
	summary := fmt.Sprintf("doctor=%s cost=%.3f", bid.DoctorID, bid.Cost)
	if !bid.Eligible {
		summary = "не участвует: " + bid.Reason
	}
	a.sink.Emit(ctx, core.Event{Action: "bid", RunID: req.RunID, TaskID: req.AuctionID,
		Subject: "auction.booking", Summary: summary})
	out, _ := json.Marshal(bid)
	return out, false
}

func (a *adapter) book(ctx context.Context, data []byte) ([]byte, bool) {
	var req BookRequest
	if err := json.Unmarshal(data, &req); err != nil {
		a.log.Warn("некорректный запрос брони", "err", err)
		res := core.Result{Step: "booking", AgentID: a.svc.agentID, Error: "invalid_request", Output: map[string]any{},
			FinishedAt: a.svc.clock.Now().Format(time.RFC3339)}
		out, _ := json.Marshal(res)
		return out, true
	}
	subject := "agent." + a.svc.agentID + ".book"
	a.sink.Emit(ctx, core.Event{Action: "won", RunID: req.RunID, TaskID: req.AuctionID, Subject: subject,
		Summary: "doctor=" + req.DoctorID + " slot=" + req.SlotStart})
	res := a.svc.Book(ctx, req)
	if res.Success {
		a.sink.Emit(ctx, core.Event{Action: "booked", RunID: req.RunID, TaskID: req.AuctionID, Subject: subject,
			Summary: fmt.Sprintf("doctor=%s slot=%s", req.DoctorID, req.SlotStart)})
	} else {
		a.sink.Emit(ctx, core.Event{Action: "failed", RunID: req.RunID, TaskID: req.AuctionID, Subject: subject,
			Summary: res.Error})
	}
	out, _ := json.Marshal(res)
	// slot_taken — штатная ситуация гонки, а не сбой агента.
	return out, !res.Success && res.Error != "slot_taken"
}
