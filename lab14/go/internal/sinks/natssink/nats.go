// Пакет natssink — сток NATS JetStream (docs/ARCHITECTURE.md, разделы 4 и 7): поток VITALS,
// субъекты vitals.raw.<device_id> и vitals.agg.<device_id>, дедупликация по Nats-Msg-Id.
package natssink

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks"
)

const (
	StreamName   = "VITALS"
	StreamMaxAge = 24 * time.Hour
	// maxPending — предел неподтверждённых асинхронных публикаций; при превышении
	// PublishMsgAsync блокируется (обратное давление на конвейер).
	maxPending = 5000
)

// RawSubject / AggSubject — субъекты из контракта. Символы, недопустимые в токене
// субъекта, заменяются на «_».
func RawSubject(device string) string { return "vitals.raw." + token(device) }
func AggSubject(device string) string { return "vitals.agg." + token(device) }

func token(s string) string {
	return strings.Map(func(r rune) rune {
		switch r {
		case '.', '*', '>', ' ', '\t', '\r', '\n':
			return '_'
		}
		return r
	}, s)
}

// RawMsgID — Nats-Msg-Id для raw: <device_id>-<seq>.
func RawMsgID(r model.Reading) string { return fmt.Sprintf("%s-%d", r.DeviceID, r.Seq) }

// AggMsgID — Nats-Msg-Id для agg: <device_id>-<metric>-<window_start>.
func AggMsgID(a model.Agg) string {
	return fmt.Sprintf("%s-%s-%s", a.DeviceID, a.Metric, a.WindowStart.UTC().Format("2006-01-02T15:04:05Z"))
}

// StreamConfig — конфигурация потока VITALS (отдельно от сети).
func StreamConfig() jetstream.StreamConfig {
	return jetstream.StreamConfig{
		Name:      StreamName,
		Subjects:  []string{"vitals.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    StreamMaxAge,
		Storage:   jetstream.FileStorage,
	}
}

type pending struct {
	subject, id string
	data        []byte
	fut         jetstream.PubAckFuture
}

// Sink публикует асинхронно; Flush дожидается всех подтверждений. Неподтверждённые
// сообщения публикуются повторно с тем же Msg-Id (дубликат отсечёт сервер), а Flush
// возвращает ошибку — ядро повторит его, и чекпоинт не сдвинется.
type Sink struct {
	nc      *nats.Conn
	js      jetstream.JetStream
	waiting []pending

	rawBytes, aggBytes, rawMsgs, aggMsgs atomic.Int64
	acks, ackErrors, dupAcks, republish  atomic.Int64
	inflight                             atomic.Int64
}

// Factory — для sinks.Registry. Соединение создаётся здесь, не при импорте.
func Factory(env sinks.Env) (sinks.Sink, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	return Dial(ctx, env.Config.NatsURL, env.Config.CollectorID)
}

// Dial подключается и идемпотентно создаёт поток VITALS.
func Dial(ctx context.Context, url, name string) (*Sink, error) {
	nc, err := nats.Connect(url, nats.Name(name), nats.MaxReconnects(-1),
		nats.ReconnectWait(500*time.Millisecond), nats.Timeout(5*time.Second))
	if err != nil {
		return nil, err
	}
	js, err := jetstream.New(nc, jetstream.WithPublishAsyncMaxPending(maxPending))
	if err != nil {
		nc.Close()
		return nil, err
	}
	// CreateOrUpdate идемпотентен: повторный запуск и второй сборщик не мешают друг другу
	if _, err := js.CreateOrUpdateStream(ctx, StreamConfig()); err != nil {
		nc.Close()
		return nil, fmt.Errorf("поток %s: %w", StreamName, err)
	}
	return &Sink{nc: nc, js: js}, nil
}

func (s *Sink) publish(subject, id string, data []byte) error {
	fut, err := s.js.PublishAsync(subject, data, jetstream.WithMsgID(id))
	if err != nil {
		return err
	}
	s.inflight.Add(1)
	s.waiting = append(s.waiting, pending{subject, id, data, fut})
	return nil
}

func (s *Sink) WriteRaw(_ context.Context, batch []model.Reading) error {
	for _, r := range batch {
		data := r.AppendJSON(make([]byte, 0, 200))
		if err := s.publish(RawSubject(r.DeviceID), RawMsgID(r), data); err != nil {
			return err
		}
		s.rawBytes.Add(int64(len(data)))
		s.rawMsgs.Add(1)
	}
	return nil
}

func (s *Sink) WriteAgg(_ context.Context, batch []model.Agg) error {
	for _, a := range batch {
		data := a.AppendJSON(make([]byte, 0, 320))
		if err := s.publish(AggSubject(a.DeviceID), AggMsgID(a), data); err != nil {
			return err
		}
		s.aggBytes.Add(int64(len(data)))
		s.aggMsgs.Add(1)
	}
	return nil
}

// Flush ждёт подтверждения всех опубликованных сообщений.
func (s *Sink) Flush(ctx context.Context) error {
	var failed []pending
	var firstErr error
	for i, p := range s.waiting {
		select {
		case ack := <-p.fut.Ok():
			s.acks.Add(1)
			if ack != nil && ack.Duplicate {
				s.dupAcks.Add(1)
			}
		case err := <-p.fut.Err():
			s.ackErrors.Add(1)
			if firstErr == nil {
				firstErr = err
			}
			failed = append(failed, p)
		case <-ctx.Done():
			s.waiting = append(failed, s.waiting[i:]...)
			return ctx.Err()
		}
		s.inflight.Add(-1)
	}
	s.waiting = s.waiting[:0]
	if firstErr == nil {
		return nil
	}
	// повторная публикация: сообщения остаются в ожидании до следующего Flush
	for _, p := range failed {
		fut, err := s.js.PublishAsync(p.subject, p.data, jetstream.WithMsgID(p.id))
		if err != nil {
			return errors.Join(firstErr, err)
		}
		s.republish.Add(1)
		s.inflight.Add(1)
		p.fut = fut
		s.waiting = append(s.waiting, p)
	}
	return fmt.Errorf("nats: %d сообщений не подтверждено: %w", len(failed), firstErr)
}

func (s *Sink) Close() error {
	// Drain дожидается отправки буферизованного и закрывает соединение
	if err := s.nc.Drain(); err != nil {
		s.nc.Close()
		return err
	}
	return nil
}

func (s *Sink) Stats() sinks.Stats {
	return sinks.Stats{RawBytes: s.rawBytes.Load(), AggBytes: s.aggBytes.Load(),
		RawRecords: s.rawMsgs.Load(), AggRecords: s.aggMsgs.Load()}
}

// Extra — дополнительные счётчики для /metrics.
func (s *Sink) Extra() map[string]int64 {
	return map[string]int64{
		"messages_published": s.rawMsgs.Load() + s.aggMsgs.Load(),
		"acks":               s.acks.Load(),
		"ack_duplicates":     s.dupAcks.Load(),
		"ack_errors":         s.ackErrors.Load(),
		"republished":        s.republish.Load(),
		"inflight":           s.inflight.Load(),
	}
}
