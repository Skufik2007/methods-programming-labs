package autoscale

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/bus"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
)

// NATS — Observer (JetStream) и Emitter (core NATS) на одном соединении.
type NATS struct {
	nc    *nats.Conn
	js    jetstream.JetStream
	clock core.Clock
	log   *slog.Logger
}

// ConnectNATS подключается к NATS, повторяя попытки.
func ConnectNATS(ctx context.Context, url string, clock core.Clock, log *slog.Logger) (*NATS, error) {
	var (
		nc  *nats.Conn
		err error
	)
	for attempt := 1; attempt <= 10; attempt++ {
		nc, err = nats.Connect(url, nats.Name(eventSource), nats.MaxReconnects(-1),
			nats.ReconnectWait(time.Second), nats.Timeout(3*time.Second))
		if err == nil {
			break
		}
		log.Warn("NATS недоступен, повтор", "attempt", attempt, "err", err)
		select {
		case <-time.After(time.Second):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if err != nil {
		return nil, fmt.Errorf("подключение к NATS %s: %w", url, err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, err
	}
	return &NATS{nc: nc, js: js, clock: clock, log: log}, nil
}

func (n *NATS) Close() {
	if err := n.nc.Drain(); err != nil {
		n.nc.Close()
	}
}

// Backlog реализует Observer: num_pending + num_ack_pending консьюмера step-<step>.
func (n *NATS) Backlog(ctx context.Context, step string) (int, bool, error) {
	cons, err := n.js.Consumer(ctx, bus.StreamName, "step-"+step)
	if err != nil {
		if errors.Is(err, jetstream.ErrConsumerNotFound) || errors.Is(err, jetstream.ErrStreamNotFound) {
			return 0, false, nil
		}
		return 0, false, err
	}
	info, err := cons.Info(ctx)
	if err != nil {
		if errors.Is(err, jetstream.ErrConsumerNotFound) {
			return 0, false, nil
		}
		return 0, false, err
	}
	return int(info.NumPending) + info.NumAckPending, true, nil
}

// Emit реализует Emitter: событие в events.autoscaler с trace_id/span_id и traceparent.
func (n *NATS) Emit(ctx context.Context, e core.Event) {
	if e.TS == "" {
		e.TS = n.clock.Now().Format(time.RFC3339)
	}
	if e.Source == "" {
		e.Source = eventSource
	}
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		e.TraceID, e.SpanID = sc.TraceID().String(), sc.SpanID().String()
	}
	data, err := json.Marshal(e)
	if err == nil {
		msg := &nats.Msg{Subject: "events." + e.Source, Data: data, Header: nats.Header{}}
		propagation.TraceContext{}.Inject(ctx, propagation.HeaderCarrier(msg.Header))
		err = n.nc.PublishMsg(msg)
	}
	if err != nil {
		n.log.Debug("не удалось опубликовать событие", "action", e.Action, "err", err)
	}
}
