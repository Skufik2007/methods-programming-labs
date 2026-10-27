// Пакет flightsink — сток Arrow Flight (docs/ARCHITECTURE.md, раздел 6).
//
// Этот файл — чистая конвертация model → Arrow без сети: схемы строго из контракта,
// словарные строковые столбцы, нарезка на батчи.
package flightsink

import (
	"github.com/apache/arrow-go/v18/arrow"
	"github.com/apache/arrow-go/v18/arrow/array"
	"github.com/apache/arrow-go/v18/arrow/memory"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

// BatchRows — размер RecordBatch в DoGet.
const BatchRows = 8192

var (
	tsType   = &arrow.TimestampType{Unit: arrow.Millisecond, TimeZone: "UTC"}
	dictType = &arrow.DictionaryType{IndexType: arrow.PrimitiveTypes.Int32, ValueType: arrow.BinaryTypes.String}
)

// RawSchema — схема raw из раздела 6. Допускают null только value и battery.
func RawSchema() *arrow.Schema {
	return arrow.NewSchema([]arrow.Field{
		{Name: "device_id", Type: dictType},
		{Name: "patient_id", Type: dictType},
		{Name: "seq", Type: arrow.PrimitiveTypes.Int64},
		{Name: "ts", Type: tsType},
		{Name: "metric", Type: dictType},
		{Name: "value", Type: arrow.PrimitiveTypes.Float64, Nullable: true},
		{Name: "unit", Type: arrow.BinaryTypes.String},
		{Name: "battery", Type: arrow.PrimitiveTypes.Int16, Nullable: true},
	}, nil)
}

// AggSchema — схема agg (раздел 2.4). При count == 0 min/max/mean/last равны null.
func AggSchema() *arrow.Schema {
	f64 := arrow.PrimitiveTypes.Float64
	return arrow.NewSchema([]arrow.Field{
		{Name: "window_start", Type: tsType},
		{Name: "window_end", Type: tsType},
		{Name: "device_id", Type: dictType},
		{Name: "patient_id", Type: dictType},
		{Name: "metric", Type: dictType},
		{Name: "count", Type: arrow.PrimitiveTypes.Int64},
		{Name: "invalid", Type: arrow.PrimitiveTypes.Int64},
		{Name: "sum", Type: f64},
		{Name: "min", Type: f64, Nullable: true},
		{Name: "max", Type: f64, Nullable: true},
		{Name: "mean", Type: f64, Nullable: true},
		{Name: "last", Type: f64, Nullable: true},
		{Name: "collector_id", Type: dictType},
	}, nil)
}

// dict — накопитель словаря столбца: один словарь на весь результат DoGet, поэтому
// во всех батчах он одинаков (поток IPC не требует замен словаря).
type dict struct {
	idx  map[string]int32
	vals []string
}

func newDict() *dict { return &dict{idx: map[string]int32{}} }

func (d *dict) id(s string) int32 {
	if i, ok := d.idx[s]; ok {
		return i
	}
	i := int32(len(d.vals))
	d.idx[s] = i
	d.vals = append(d.vals, s)
	return i
}

func (d *dict) array(mem memory.Allocator) arrow.Array {
	b := array.NewStringBuilder(mem)
	defer b.Release()
	b.AppendValues(d.vals, nil)
	return b.NewArray()
}

// dictCol собирает столбец-словарь из индексов [lo, hi) общего среза идентификаторов.
func dictCol(mem memory.Allocator, ids []int32, lo, hi int, values arrow.Array) arrow.Array {
	b := array.NewInt32Builder(mem)
	defer b.Release()
	b.AppendValues(ids[lo:hi], nil)
	idx := b.NewArray()
	defer idx.Release()
	return array.NewDictionaryArray(dictType, idx, values)
}

func tsMs(t interface{ UnixMilli() int64 }) arrow.Timestamp { return arrow.Timestamp(t.UnixMilli()) }

// RawRecords превращает показания в батчи по batchRows строк (batchRows <= 0 — BatchRows).
// Вызывающий освобождает записи (Release).
func RawRecords(mem memory.Allocator, rows []model.Reading, batchRows int) []arrow.Record {
	if batchRows <= 0 {
		batchRows = BatchRows
	}
	schema := RawSchema()
	dDev, dPat, dMet := newDict(), newDict(), newDict()
	devIDs := make([]int32, len(rows))
	patIDs := make([]int32, len(rows))
	metIDs := make([]int32, len(rows))
	for i, r := range rows {
		devIDs[i], patIDs[i], metIDs[i] = dDev.id(r.DeviceID), dPat.id(r.PatientID), dMet.id(r.Metric)
	}
	vDev, vPat, vMet := dDev.array(mem), dPat.array(mem), dMet.array(mem)
	defer vDev.Release()
	defer vPat.Release()
	defer vMet.Release()

	var out []arrow.Record
	for lo := 0; lo < len(rows); lo += batchRows {
		hi := min(lo+batchRows, len(rows))
		seq := array.NewInt64Builder(mem)
		ts := array.NewTimestampBuilder(mem, tsType)
		val := array.NewFloat64Builder(mem)
		unit := array.NewStringBuilder(mem)
		bat := array.NewInt16Builder(mem)
		for _, r := range rows[lo:hi] {
			seq.Append(r.Seq)
			ts.Append(tsMs(r.Ts))
			if r.Value == nil {
				val.AppendNull()
			} else {
				val.Append(*r.Value)
			}
			unit.Append(r.Unit)
			if r.Battery == nil {
				bat.AppendNull()
			} else {
				bat.Append(int16(*r.Battery))
			}
		}
		cols := []arrow.Array{
			dictCol(mem, devIDs, lo, hi, vDev), dictCol(mem, patIDs, lo, hi, vPat),
			seq.NewArray(), ts.NewArray(), dictCol(mem, metIDs, lo, hi, vMet),
			val.NewArray(), unit.NewArray(), bat.NewArray(),
		}
		seq.Release()
		ts.Release()
		val.Release()
		unit.Release()
		bat.Release()
		out = append(out, newRecord(schema, cols, hi-lo))
	}
	return out
}

// AggRecords — то же для оконных агрегатов.
func AggRecords(mem memory.Allocator, rows []model.Agg, batchRows int) []arrow.Record {
	if batchRows <= 0 {
		batchRows = BatchRows
	}
	schema := AggSchema()
	dDev, dPat, dMet, dCol := newDict(), newDict(), newDict(), newDict()
	devIDs := make([]int32, len(rows))
	patIDs := make([]int32, len(rows))
	metIDs := make([]int32, len(rows))
	colIDs := make([]int32, len(rows))
	for i, r := range rows {
		devIDs[i], patIDs[i] = dDev.id(r.DeviceID), dPat.id(r.PatientID)
		metIDs[i], colIDs[i] = dMet.id(r.Metric), dCol.id(r.CollectorID)
	}
	vDev, vPat, vMet, vCol := dDev.array(mem), dPat.array(mem), dMet.array(mem), dCol.array(mem)
	defer vDev.Release()
	defer vPat.Release()
	defer vMet.Release()
	defer vCol.Release()

	var out []arrow.Record
	for lo := 0; lo < len(rows); lo += batchRows {
		hi := min(lo+batchRows, len(rows))
		ws := array.NewTimestampBuilder(mem, tsType)
		we := array.NewTimestampBuilder(mem, tsType)
		cnt := array.NewInt64Builder(mem)
		inv := array.NewInt64Builder(mem)
		sum := array.NewFloat64Builder(mem)
		fl := [4]*array.Float64Builder{} // min, max, mean, last
		for i := range fl {
			fl[i] = array.NewFloat64Builder(mem)
		}
		for _, r := range rows[lo:hi] {
			ws.Append(tsMs(r.WindowStart))
			we.Append(tsMs(r.WindowEnd))
			cnt.Append(r.Count)
			inv.Append(r.Invalid)
			sum.Append(r.Sum)
			for i, v := range [4]float64{r.Min, r.Max, r.Mean, r.Last} {
				if r.Count == 0 {
					fl[i].AppendNull()
				} else {
					fl[i].Append(v)
				}
			}
		}
		cols := []arrow.Array{
			ws.NewArray(), we.NewArray(),
			dictCol(mem, devIDs, lo, hi, vDev), dictCol(mem, patIDs, lo, hi, vPat), dictCol(mem, metIDs, lo, hi, vMet),
			cnt.NewArray(), inv.NewArray(), sum.NewArray(),
			fl[0].NewArray(), fl[1].NewArray(), fl[2].NewArray(), fl[3].NewArray(),
			dictCol(mem, colIDs, lo, hi, vCol),
		}
		ws.Release()
		we.Release()
		cnt.Release()
		inv.Release()
		sum.Release()
		for _, b := range fl {
			b.Release()
		}
		out = append(out, newRecord(schema, cols, hi-lo))
	}
	return out
}

// newRecord создаёт запись и отдаёт ей владение столбцами.
func newRecord(schema *arrow.Schema, cols []arrow.Array, rows int) arrow.Record {
	rec := array.NewRecord(schema, cols, int64(rows))
	for _, c := range cols {
		c.Release()
	}
	return rec
}
