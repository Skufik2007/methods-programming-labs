package flightsink

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/apache/arrow-go/v18/arrow"
	"github.com/apache/arrow-go/v18/arrow/flight"
	"github.com/apache/arrow-go/v18/arrow/ipc"
	"github.com/apache/arrow-go/v18/arrow/memory"
	"github.com/apache/arrow-go/v18/arrow/util"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks"
)

// Ticket — тикет DoGet (JSON). since пустой — отдать весь буфер.
type Ticket struct {
	Kind  string `json:"kind"` // raw | agg
	Since string `json:"since,omitempty"`
}

// ParseTicket разбирает тикет; since — по RFC 3339 (нулевое время, если не задан).
func ParseTicket(b []byte) (kind string, since time.Time, err error) {
	var t Ticket
	if err = json.Unmarshal(b, &t); err != nil {
		return "", since, fmt.Errorf("тикет не JSON: %w", err)
	}
	if t.Kind != "raw" && t.Kind != "agg" {
		return "", since, fmt.Errorf("kind должен быть raw или agg, получено %q", t.Kind)
	}
	if t.Since != "" {
		if since, err = time.Parse(time.RFC3339, t.Since); err != nil {
			return "", since, fmt.Errorf("since: %w", err)
		}
	}
	return t.Kind, since, nil
}

// ring — буфер записей с вытеснением по времени поступления (не по ts: у устройства
// с неверными часами ts может быть из будущего, и запись никогда бы не вытеснилась).
type ring[T any] struct {
	items []entry[T]
}

type entry[T any] struct {
	at time.Time
	v  T
}

func (r *ring[T]) add(at time.Time, vs []T) {
	for _, v := range vs {
		r.items = append(r.items, entry[T]{at, v})
	}
}

// evict выбрасывает записи старше cutoff; записи идут по времени поступления.
func (r *ring[T]) evict(cutoff time.Time) {
	n := 0
	for n < len(r.items) && r.items[n].at.Before(cutoff) {
		n++
	}
	if n > 0 {
		// копируем хвост в новый срез, чтобы не удерживать память вытесненного
		r.items = append([]entry[T](nil), r.items[n:]...)
	}
}

// Sink — кольцевой буфер последних записей и Flight-сервер поверх него.
type Sink struct {
	clk       clock.Clock
	log       *slog.Logger
	retention time.Duration
	mem       memory.Allocator

	stageRaw []model.Reading // записанное, но ещё не подтверждённое Flush
	stageAgg []model.Agg

	mu   sync.RWMutex
	raw  ring[model.Reading]
	agg  ring[model.Agg]
	last time.Time // последняя чистка

	srv  flight.Server
	addr string // фактический адрес прослушивания
	adv  string // адрес для публикации в etcd

	rawRows, aggRows, rawServedBytes, aggServedBytes atomic.Int64
	doGets, rowsServed, doGetErrors                  atomic.Int64
}

// Factory — для sinks.Registry.
func Factory(env sinks.Env) (sinks.Sink, error) {
	c := env.Config
	return New(c.FlightAddr, c.FlightAdvertise, c.FlightRetention, env.Clock, env.Log)
}

// New запускает Flight-сервер на listenAddr (host:port; порт 0 — любой свободный).
func New(listenAddr, advertise string, retention time.Duration, clk clock.Clock, log *slog.Logger) (*Sink, error) {
	ln, err := net.Listen("tcp", listenAddr)
	if err != nil {
		return nil, fmt.Errorf("flight: %w", err)
	}
	s := &Sink{clk: clk, log: log, retention: retention, mem: memory.DefaultAllocator, addr: ln.Addr().String()}
	s.adv = advertise
	if s.adv == "" {
		_, port, _ := net.SplitHostPort(s.addr)
		host, _, _ := net.SplitHostPort(listenAddr)
		if host == "" || host == "0.0.0.0" || host == "::" {
			host = "127.0.0.1" // в кластере задайте FLIGHT_ADVERTISE
		}
		s.adv = net.JoinHostPort(host, port)
	}
	s.srv = flight.NewServerWithMiddleware(nil)
	s.srv.RegisterFlightService(&service{s: s})
	s.srv.InitListener(ln)
	go func() {
		if err := s.srv.Serve(); err != nil {
			log.Error("flight: сервер остановился", "err", err)
		}
	}()
	log.Info("flight: сервер запущен", "addr", s.addr, "advertise", s.adv, "retention", retention.String())
	return s, nil
}

// Addr — адрес, который нужно публиковать в etcd (flight_addr).
func (s *Sink) Addr() string { return s.adv }

func (s *Sink) WriteRaw(_ context.Context, batch []model.Reading) error {
	s.stageRaw = append(s.stageRaw, batch...)
	return nil
}

func (s *Sink) WriteAgg(_ context.Context, batch []model.Agg) error {
	s.stageAgg = append(s.stageAgg, batch...)
	return nil
}

// Flush переносит подготовленное в буфер, который видит DoGet.
func (s *Sink) Flush(context.Context) error {
	now := s.clk.Now()
	s.mu.Lock()
	s.raw.add(now, s.stageRaw)
	s.agg.add(now, s.stageAgg)
	s.rawRows.Add(int64(len(s.stageRaw)))
	s.aggRows.Add(int64(len(s.stageAgg)))
	s.evictLocked(now)
	s.mu.Unlock()
	s.stageRaw, s.stageAgg = s.stageRaw[:0], s.stageAgg[:0]
	return nil
}

func (s *Sink) evictLocked(now time.Time) {
	cutoff := now.Add(-s.retention)
	s.raw.evict(cutoff)
	s.agg.evict(cutoff)
	s.last = now
}

func (s *Sink) Close() error {
	s.srv.Shutdown()
	return nil
}

func (s *Sink) Stats() sinks.Stats {
	return sinks.Stats{RawBytes: s.rawServedBytes.Load(), AggBytes: s.aggServedBytes.Load(),
		RawRecords: s.rawRows.Load(), AggRecords: s.aggRows.Load()}
}

// Extra — счётчики для /metrics. Bytes в Stats — отданные по DoGet, Records — принятые в буфер.
func (s *Sink) Extra() map[string]int64 {
	s.mu.RLock()
	nr, na := len(s.raw.items), len(s.agg.items)
	s.mu.RUnlock()
	return map[string]int64{
		"do_get_calls": s.doGets.Load(), "do_get_errors": s.doGetErrors.Load(),
		"rows_served": s.rowsServed.Load(), "buffered_raw": int64(nr), "buffered_agg": int64(na),
	}
}

// snapshot возвращает копию записей с ts >= since.
func (s *Sink) snapshotRaw(since time.Time) []model.Reading {
	s.mu.Lock()
	s.evictLocked(s.clk.Now())
	out := make([]model.Reading, 0, len(s.raw.items))
	for _, e := range s.raw.items {
		if since.IsZero() || !e.v.Ts.Before(since) {
			out = append(out, e.v)
		}
	}
	s.mu.Unlock()
	return out
}

// Для agg фильтр по window_start.
func (s *Sink) snapshotAgg(since time.Time) []model.Agg {
	s.mu.Lock()
	s.evictLocked(s.clk.Now())
	out := make([]model.Agg, 0, len(s.agg.items))
	for _, e := range s.agg.items {
		if since.IsZero() || !e.v.WindowStart.Before(since) {
			out = append(out, e.v)
		}
	}
	s.mu.Unlock()
	return out
}

// service — gRPC-сервис Flight.
type service struct {
	flight.BaseFlightServer
	s *Sink
}

func (v *service) info(kind string) (*flight.FlightInfo, error) {
	schema := RawSchema()
	rows := int64(0)
	v.s.mu.RLock()
	rows = int64(len(v.s.raw.items))
	if kind == "agg" {
		schema, rows = AggSchema(), int64(len(v.s.agg.items))
	}
	v.s.mu.RUnlock()
	tk, _ := json.Marshal(Ticket{Kind: kind})
	return &flight.FlightInfo{
		Schema:           flight.SerializeSchema(schema, v.s.mem),
		FlightDescriptor: &flight.FlightDescriptor{Type: flight.DescriptorPATH, Path: []string{kind}},
		Endpoint: []*flight.FlightEndpoint{{
			Ticket:   &flight.Ticket{Ticket: tk},
			Location: []*flight.Location{{Uri: "grpc+tcp://" + v.s.adv}},
		}},
		TotalRecords: rows,
		TotalBytes:   -1,
	}, nil
}

func (v *service) ListFlights(_ *flight.Criteria, stream flight.FlightService_ListFlightsServer) error {
	for _, kind := range []string{"raw", "agg"} {
		fi, err := v.info(kind)
		if err != nil {
			return err
		}
		if err := stream.Send(fi); err != nil {
			return err
		}
	}
	return nil
}

func (v *service) GetFlightInfo(_ context.Context, d *flight.FlightDescriptor) (*flight.FlightInfo, error) {
	if d == nil || len(d.Path) != 1 || (d.Path[0] != "raw" && d.Path[0] != "agg") {
		return nil, status.Error(codes.NotFound, "доступны наборы raw и agg (path=[raw] или [agg])")
	}
	return v.info(d.Path[0])
}

func (v *service) DoGet(tk *flight.Ticket, stream flight.FlightService_DoGetServer) error {
	s := v.s
	s.doGets.Add(1)
	kind, since, err := ParseTicket(tk.GetTicket())
	if err != nil {
		s.doGetErrors.Add(1)
		return status.Error(codes.InvalidArgument, err.Error())
	}
	schema, recs := RawSchema(), []arrow.Record(nil)
	bytesCounter := &s.rawServedBytes
	if kind == "agg" {
		schema, bytesCounter = AggSchema(), &s.aggServedBytes
		recs = AggRecords(s.mem, s.snapshotAgg(since), BatchRows)
	} else {
		recs = RawRecords(s.mem, s.snapshotRaw(since), BatchRows)
	}
	w := flight.NewRecordWriter(stream, ipc.WithSchema(schema), ipc.WithAllocator(s.mem))
	defer w.Close()
	for _, rec := range recs {
		if err == nil {
			if err = w.Write(rec); err == nil {
				s.rowsServed.Add(rec.NumRows())
				bytesCounter.Add(int64(util.TotalRecordSize(rec)))
			}
		}
		rec.Release() // освобождаем и после ошибки записи
	}
	if err != nil {
		s.doGetErrors.Add(1)
	}
	return err
}
