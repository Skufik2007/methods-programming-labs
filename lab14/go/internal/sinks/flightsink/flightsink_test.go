package flightsink

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/apache/arrow-go/v18/arrow"
	"github.com/apache/arrow-go/v18/arrow/array"
	"github.com/apache/arrow-go/v18/arrow/flight"
	"github.com/apache/arrow-go/v18/arrow/memory"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

var t0 = time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)

func fp(v float64) *float64 { return &v }
func ip(v int) *int         { return &v }

func isDict(dt arrow.DataType) bool {
	d, ok := dt.(*arrow.DictionaryType)
	return ok && d.IndexType.ID() == arrow.INT32 && d.ValueType.ID() == arrow.STRING
}

func TestRawSchemaMatchesContract(t *testing.T) {
	s := RawSchema()
	want := []struct {
		name     string
		dict     bool
		typ      arrow.Type
		nullable bool
	}{
		{"device_id", true, 0, false},
		{"patient_id", true, 0, false},
		{"seq", false, arrow.INT64, false},
		{"ts", false, arrow.TIMESTAMP, false},
		{"metric", true, 0, false},
		{"value", false, arrow.FLOAT64, true},
		{"unit", false, arrow.STRING, false},
		{"battery", false, arrow.INT16, true},
	}
	if s.NumFields() != len(want) {
		t.Fatalf("полей %d, ожидалось %d", s.NumFields(), len(want))
	}
	for i, w := range want {
		f := s.Field(i)
		if f.Name != w.name {
			t.Fatalf("поле %d: %s, ожидалось %s", i, f.Name, w.name)
		}
		if w.dict {
			if !isDict(f.Type) {
				t.Errorf("%s: ожидался dictionary<int32, utf8>, получен %s", w.name, f.Type)
			}
		} else if f.Type.ID() != w.typ {
			t.Errorf("%s: тип %s, ожидался id %v", w.name, f.Type, w.typ)
		}
		if f.Nullable != w.nullable {
			t.Errorf("%s: nullable=%v, ожидалось %v", w.name, f.Nullable, w.nullable)
		}
	}
	if ts := s.Field(3).Type.(*arrow.TimestampType); ts.Unit != arrow.Millisecond || ts.TimeZone != "UTC" {
		t.Errorf("ts: %s, ожидалось timestamp[ms, UTC]", ts)
	}
}

func TestAggSchemaMatchesContract(t *testing.T) {
	s := AggSchema()
	names := []string{"window_start", "window_end", "device_id", "patient_id", "metric", "count",
		"invalid", "sum", "min", "max", "mean", "last", "collector_id"}
	if s.NumFields() != len(names) {
		t.Fatalf("полей %d, ожидалось %d", s.NumFields(), len(names))
	}
	for i, n := range names {
		if s.Field(i).Name != n {
			t.Fatalf("поле %d: %s, ожидалось %s", i, s.Field(i).Name, n)
		}
	}
	for _, i := range []int{8, 9, 10, 11} { // min, max, mean, last допускают null
		if !s.Field(i).Nullable {
			t.Errorf("%s должен быть nullable", s.Field(i).Name)
		}
	}
	if s.Field(7).Nullable {
		t.Error("sum не должен быть nullable")
	}
	if !isDict(s.Field(12).Type) {
		t.Error("collector_id должен быть словарным")
	}
}

func rawRows(n int) []model.Reading {
	out := make([]model.Reading, n)
	for i := range out {
		r := model.Reading{DeviceID: "dev-" + string(rune('0'+i%3)), PatientID: "p", Seq: int64(i + 1),
			Ts: t0.Add(time.Duration(i) * time.Millisecond), Metric: "heart_rate", Value: fp(70), Unit: "bpm", Battery: ip(80)}
		if i%5 == 0 {
			r.Value = nil
		}
		if i%7 == 0 {
			r.Battery = nil
		}
		out[i] = r
	}
	return out
}

func TestRawRecordsBatchingAndNulls(t *testing.T) {
	mem := memory.NewCheckedAllocator(memory.NewGoAllocator())
	recs := RawRecords(mem, rawRows(20000), 0)
	defer func() {
		for _, r := range recs {
			r.Release()
		}
	}()
	var sizes []int64
	total := int64(0)
	for _, r := range recs {
		sizes = append(sizes, r.NumRows())
		total += r.NumRows()
		if !r.Schema().Equal(RawSchema()) {
			t.Fatal("схема батча не совпадает с контрактом")
		}
	}
	if total != 20000 {
		t.Fatalf("строк %d", total)
	}
	if len(sizes) != 3 || sizes[0] != BatchRows || sizes[1] != BatchRows || sizes[2] != 20000-2*BatchRows {
		t.Fatalf("нарезка на батчи %v, ожидалось 8192/8192/3616", sizes)
	}
	valCol := recs[0].Column(5).(*array.Float64)
	if !valCol.IsNull(0) || valCol.IsNull(1) {
		t.Errorf("value: null в позиции 0 (i%%5==0) и не-null в 1")
	}
	batCol := recs[0].Column(7).(*array.Int16)
	if !batCol.IsNull(0) {
		t.Error("battery: ожидался null в позиции 0 (i%7==0)")
	}
	// словарь один на весь результат: идентификаторы у всех батчей одинаковые
	d0 := recs[0].Column(0).(*array.Dictionary).Dictionary().(*array.String)
	d1 := recs[1].Column(0).(*array.Dictionary).Dictionary().(*array.String)
	if d0.Len() != d1.Len() {
		t.Fatalf("словарь различается между батчами: %d и %d", d0.Len(), d1.Len())
	}
}

func TestAggRecordsNullsWhenNoValidValues(t *testing.T) {
	mem := memory.NewGoAllocator()
	rows := []model.Agg{
		{WindowStart: t0, WindowEnd: t0.Add(10 * time.Second), DeviceID: "dev-0001", PatientID: "p",
			Metric: "spo2", Count: 0, Invalid: 3, CollectorID: "c1"},
		{WindowStart: t0, WindowEnd: t0.Add(10 * time.Second), DeviceID: "dev-0002", PatientID: "p",
			Metric: "spo2", Count: 2, Invalid: 0, Sum: 190, Min: 94, Max: 96, Mean: 95, Last: 96, CollectorID: "c1"},
	}
	recs := RawRecordsAgg(mem, rows)
	defer func() {
		for _, r := range recs {
			r.Release()
		}
	}()
	if len(recs) != 1 || recs[0].NumRows() != 2 {
		t.Fatalf("батчей %d", len(recs))
	}
	for col := 8; col <= 11; col++ {
		c := recs[0].Column(col).(*array.Float64)
		if !c.IsNull(0) {
			t.Errorf("колонка %s: ожидался null при count=0", recs[0].ColumnName(col))
		}
		if c.IsNull(1) {
			t.Errorf("колонка %s: ожидалось значение при count=2", recs[0].ColumnName(col))
		}
	}
	if recs[0].Column(7).(*array.Float64).IsNull(0) {
		t.Error("sum не должен быть null")
	}
}

// RawRecordsAgg — короткая обёртка над AggRecords для читаемости теста.
func RawRecordsAgg(mem memory.Allocator, rows []model.Agg) []arrow.Record {
	return AggRecords(mem, rows, 0)
}

func TestDoGetOverFlightClient(t *testing.T) {
	sink, err := New("127.0.0.1:0", "", 15*time.Minute, clock.NewFake(t0), slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	ctx := context.Background()
	if err := sink.WriteRaw(ctx, rawRows(9000)); err != nil {
		t.Fatal(err)
	}
	if err := sink.Flush(ctx); err != nil {
		t.Fatal(err)
	}

	cli, err := flight.NewClientWithMiddleware(sink.addr, nil, nil, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()

	t.Run("raw batches", func(t *testing.T) {
		stream, err := cli.DoGet(ctx, &flight.Ticket{Ticket: []byte(`{"kind":"raw"}`)})
		if err != nil {
			t.Fatal(err)
		}
		rd, err := flight.NewRecordReader(stream)
		if err != nil {
			t.Fatal(err)
		}
		defer rd.Release()
		rows, batches := int64(0), 0
		for rd.Next() {
			rows += rd.RecordBatch().NumRows()
			batches++
		}
		if rd.Err() != nil && rd.Err() != io.EOF {
			t.Fatal(rd.Err())
		}
		if rows != 9000 || batches != 2 {
			t.Fatalf("получено %d строк в %d батчах, ожидалось 9000 в 2", rows, batches)
		}
	})

	t.Run("bad kind is InvalidArgument", func(t *testing.T) {
		stream, err := cli.DoGet(ctx, &flight.Ticket{Ticket: []byte(`{"kind":"xml"}`)})
		if err == nil {
			_, err = flight.NewRecordReader(stream)
		}
		if err == nil || !strings.Contains(err.Error(), "kind") {
			t.Fatalf("ожидалась ошибка kind, получено %v", err)
		}
	})
}

func TestParseTicket(t *testing.T) {
	kind, since, err := ParseTicket([]byte(`{"kind":"agg","since":"2026-10-13T09:30:00Z"}`))
	if err != nil || kind != "agg" || !since.Equal(t0) {
		t.Fatalf("kind=%q since=%v err=%v", kind, since, err)
	}
	if _, _, err := ParseTicket([]byte(`не json`)); err == nil {
		t.Fatal("ожидалась ошибка для не-JSON")
	}
	if _, _, err := ParseTicket([]byte(`{"kind":"raw","since":"вчера"}`)); err == nil {
		t.Fatal("ожидалась ошибка для since")
	}
}
