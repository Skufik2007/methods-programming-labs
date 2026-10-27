package natssink

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

var t0 = time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)

func fp(v float64) *float64 { return &v }

// startNATS поднимает встроенный сервер с JetStream на случайном порту в t.TempDir().
func startNATS(t *testing.T) string {
	t.Helper()
	opts := &server.Options{
		Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: t.TempDir(),
		NoLog: true, NoSigs: true,
	}
	s, err := server.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server не готов")
	}
	t.Cleanup(s.Shutdown)
	return s.ClientURL()
}

func TestSubjectsAndMsgIDs(t *testing.T) {
	t.Run("subjects", func(t *testing.T) {
		cases := []struct{ in, raw, agg string }{
			{"dev-0001", "vitals.raw.dev-0001", "vitals.agg.dev-0001"},
			{"dev.0002", "vitals.raw.dev_0002", "vitals.agg.dev_0002"},
			{"dev*x>y", "vitals.raw.dev_x_y", "vitals.agg.dev_x_y"},
		}
		for _, c := range cases {
			if got := RawSubject(c.in); got != c.raw {
				t.Errorf("RawSubject(%q) = %q, ожидалось %q", c.in, got, c.raw)
			}
			if got := AggSubject(c.in); got != c.agg {
				t.Errorf("AggSubject(%q) = %q, ожидалось %q", c.in, got, c.agg)
			}
		}
	})

	t.Run("raw msg id", func(t *testing.T) {
		r := model.Reading{DeviceID: "dev-0001", Seq: 123}
		if got := RawMsgID(r); got != "dev-0001-123" {
			t.Fatalf("RawMsgID = %q", got)
		}
	})

	t.Run("agg msg id", func(t *testing.T) {
		a := model.Agg{DeviceID: "dev-0001", Metric: "heart_rate", WindowStart: t0}
		if got := AggMsgID(a); got != "dev-0001-heart_rate-2026-10-13T09:30:00Z" {
			t.Fatalf("AggMsgID = %q", got)
		}
	})
}

func TestStreamConfig(t *testing.T) {
	c := StreamConfig()
	if c.Name != "VITALS" {
		t.Errorf("имя потока %q", c.Name)
	}
	if len(c.Subjects) != 1 || c.Subjects[0] != "vitals.>" {
		t.Errorf("субъекты %v", c.Subjects)
	}
	if c.Retention != jetstream.LimitsPolicy {
		t.Errorf("retention %v, ожидался limits", c.Retention)
	}
	if c.MaxAge != 24*time.Hour {
		t.Errorf("max_age %v", c.MaxAge)
	}
}

func dialSink(t *testing.T, url string) *Sink {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	s, err := Dial(ctx, url, "collector-1")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func streamMsgs(t *testing.T, url string) uint64 {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	st, err := js.Stream(context.Background(), StreamName)
	if err != nil {
		t.Fatal(err)
	}
	info, err := st.Info(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return info.State.Msgs
}

func TestPublishSubjectsAndDedup(t *testing.T) {
	url := startNATS(t)
	s := dialSink(t, url)
	ctx := context.Background()

	raw := []model.Reading{
		{DeviceID: "dev-0001", PatientID: "p-0001", Seq: 1, Ts: t0, Metric: "heart_rate", Value: fp(70), Unit: "bpm"},
		{DeviceID: "dev-0002", PatientID: "p-0002", Seq: 1, Ts: t0, Metric: "spo2", Value: fp(97), Unit: "%"},
	}
	if err := s.WriteRaw(ctx, raw); err != nil {
		t.Fatal(err)
	}
	if err := s.Flush(ctx); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if n := streamMsgs(t, url); n != 2 {
		t.Fatalf("в потоке %d сообщений, ожидалось 2", n)
	}

	// Повторная публикация тех же показаний: сервер отсекает по Nats-Msg-Id.
	if err := s.WriteRaw(ctx, raw[:1]); err != nil {
		t.Fatal(err)
	}
	if err := s.Flush(ctx); err != nil {
		t.Fatalf("повторный Flush: %v", err)
	}
	if n := streamMsgs(t, url); n != 2 {
		t.Fatalf("после повтора %d сообщений, ожидалось 2 (дедупликация)", n)
	}
	if d := s.Extra()["ack_duplicates"]; d != 1 {
		t.Fatalf("ack_duplicates = %d, ожидался 1", d)
	}

	// Сообщение лежит в субъекте vitals.raw.<device>.
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, _ := jetstream.New(nc)
	st, _ := js.Stream(ctx, StreamName)
	msg, err := st.GetLastMsgForSubject(ctx, "vitals.raw.dev-0002")
	if err != nil {
		t.Fatalf("сообщение по субъекту: %v", err)
	}
	if !strings.Contains(string(msg.Data), `"device_id":"dev-0002"`) {
		t.Fatalf("тело сообщения: %s", msg.Data)
	}
}

func TestPublishAggSubject(t *testing.T) {
	url := startNATS(t)
	s := dialSink(t, url)
	ctx := context.Background()
	a := model.Agg{WindowStart: t0, WindowEnd: t0.Add(10 * time.Second), DeviceID: "dev-0003",
		PatientID: "p-0003", Metric: "steps", Count: 1, Sum: 5, Min: 5, Max: 5, Mean: 5, Last: 5, CollectorID: "collector-1"}
	if err := s.WriteAgg(ctx, []model.Agg{a}); err != nil {
		t.Fatal(err)
	}
	if err := s.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, _ := jetstream.New(nc)
	st, _ := js.Stream(ctx, StreamName)
	if _, err := st.GetLastMsgForSubject(ctx, "vitals.agg.dev-0003"); err != nil {
		t.Fatalf("agg не опубликован в vitals.agg.dev-0003: %v", err)
	}
}

// Перезапуск с тем же потоком не падает: CreateOrUpdate идемпотентен.
func TestDialIdempotentStream(t *testing.T) {
	url := startNATS(t)
	dialSink(t, url)
	dialSink(t, url)
}
