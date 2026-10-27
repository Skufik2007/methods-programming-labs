package files

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

func fp(v float64) *float64 { return &v }
func ip(v int) *int         { return &v }

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestRawLineByteForByte(t *testing.T) {
	ts := time.Date(2026, 10, 13, 9, 30, 0, 250_000_000, time.UTC)
	cases := []struct {
		name string
		r    model.Reading
		want string
	}{
		{
			name: "обычное показание, 72.0 и ts с миллисекундами",
			r: model.Reading{DeviceID: "dev-0001", PatientID: "p-0001", Seq: 123, Ts: ts,
				Metric: "heart_rate", Value: fp(72), Unit: "bpm", Battery: ip(87)},
			want: `{"device_id":"dev-0001","patient_id":"p-0001","seq":123,"ts":"2026-10-13T09:30:00.250Z","metric":"heart_rate","value":72.0,"unit":"bpm","battery":87}`,
		},
		{
			name: "сбой датчика: value и battery null",
			r: model.Reading{DeviceID: "dev-0002", PatientID: "p-0002", Seq: 1, Ts: ts,
				Metric: "spo2", Value: nil, Unit: "%", Battery: nil},
			want: `{"device_id":"dev-0002","patient_id":"p-0002","seq":1,"ts":"2026-10-13T09:30:00.250Z","metric":"spo2","value":null,"unit":"%","battery":null}`,
		},
		{
			name: "пустая единица измерения (дефект)",
			r: model.Reading{DeviceID: "dev-0003", PatientID: "p-0003", Seq: 2, Ts: ts,
				Metric: "temperature", Value: fp(36.6), Unit: "", Battery: ip(0)},
			want: `{"device_id":"dev-0003","patient_id":"p-0003","seq":2,"ts":"2026-10-13T09:30:00.250Z","metric":"temperature","value":36.6,"unit":"","battery":0}`,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			clk := clock.NewFake(time.Date(2026, 10, 13, 9, 30, 1, 0, time.UTC))
			s := New(dir, "collector-1", clk)
			if err := s.WriteRaw(context.Background(), []model.Reading{c.r}); err != nil {
				t.Fatal(err)
			}
			if err := s.Flush(context.Background()); err != nil {
				t.Fatal(err)
			}
			got := readFile(t, filepath.Join(dir, "raw", "collector-1-2026101309.jsonl"))
			if got != c.want+"\n" {
				t.Fatalf("строка не совпала с контрактом\n got: %s\nwant: %s", got, c.want)
			}
			_ = s.Close()
		})
	}
}

func TestAggLineByteForByte(t *testing.T) {
	ws := time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)
	dir := t.TempDir()
	clk := clock.NewFake(ws.Add(11 * time.Second))
	s := New(dir, "collector-1", clk)
	a := model.Agg{
		WindowStart: ws, WindowEnd: ws.Add(10 * time.Second),
		DeviceID: "dev-0001", PatientID: "p-0001", Metric: "heart_rate",
		Count: 10, Invalid: 1, Sum: 731, Min: 68, Max: 79, Mean: 73.1, Last: 75,
		CollectorID: "collector-1",
	}
	if err := s.WriteAgg(context.Background(), []model.Agg{a}); err != nil {
		t.Fatal(err)
	}
	if err := s.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	want := `{"window_start":"2026-10-13T09:30:00Z","window_end":"2026-10-13T09:30:10Z","device_id":"dev-0001","patient_id":"p-0001","metric":"heart_rate","count":10,"invalid":1,"sum":731.0,"min":68.0,"max":79.0,"mean":73.1,"last":75.0,"collector_id":"collector-1"}`
	got := readFile(t, filepath.Join(dir, "agg", "collector-1-2026101309.jsonl"))
	if got != want+"\n" {
		t.Fatalf("agg не совпал с контрактом\n got: %s\nwant: %s", got, want)
	}
	_ = s.Close()
}

// Пустое окно: sum = 0.0, остальные агрегаты null.
func TestAggEmptyWindowNulls(t *testing.T) {
	ws := time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)
	dir := t.TempDir()
	s := New(dir, "c1", clock.NewFake(ws))
	a := model.Agg{WindowStart: ws, WindowEnd: ws.Add(10 * time.Second), DeviceID: "dev-0001",
		PatientID: "p-0001", Metric: "spo2", Count: 0, Invalid: 4, CollectorID: "c1"}
	_ = s.WriteAgg(context.Background(), []model.Agg{a})
	_ = s.Flush(context.Background())
	got := readFile(t, filepath.Join(dir, "agg", "c1-2026101309.jsonl"))
	for _, frag := range []string{`"sum":0.0`, `"min":null`, `"max":null`, `"mean":null`, `"last":null`} {
		if !strings.Contains(got, frag) {
			t.Errorf("нет %s в строке: %s", frag, got)
		}
	}
	_ = s.Close()
}

func TestHourRotation(t *testing.T) {
	dir := t.TempDir()
	clk := clock.NewFake(time.Date(2026, 10, 13, 9, 59, 59, 0, time.UTC))
	s := New(dir, "c1", clk)
	r := model.Reading{DeviceID: "dev-0001", PatientID: "p-0001", Seq: 1, Ts: clk.Now(), Metric: "heart_rate",
		Value: fp(70), Unit: "bpm"}
	_ = s.WriteRaw(context.Background(), []model.Reading{r})
	_ = s.Flush(context.Background())

	clk.Advance(2 * time.Second) // 10:00:01
	r.Seq = 2
	r.Ts = clk.Now()
	_ = s.WriteRaw(context.Background(), []model.Reading{r})
	_ = s.Flush(context.Background())
	_ = s.Close()

	for _, name := range []string{"c1-2026101309.jsonl", "c1-2026101310.jsonl"} {
		b := readFile(t, filepath.Join(dir, "raw", name))
		if strings.Count(b, "\n") != 1 {
			t.Fatalf("%s: ожидалась одна строка, получено %q", name, b)
		}
	}
}

func TestStatsCounted(t *testing.T) {
	dir := t.TempDir()
	s := New(dir, "c1", clock.NewFake(time.Date(2026, 10, 13, 9, 0, 0, 0, time.UTC)))
	rs := []model.Reading{
		{DeviceID: "dev-0001", PatientID: "p", Seq: 1, Ts: time.Date(2026, 10, 13, 9, 0, 0, 0, time.UTC), Metric: "steps", Value: fp(3), Unit: "count"},
	}
	_ = s.WriteRaw(context.Background(), rs)
	_ = s.Flush(context.Background())
	st := s.Stats()
	if st.RawRecords != 1 || st.RawBytes <= 0 {
		t.Fatalf("статистика: %+v", st)
	}
	_ = s.Close()
}
