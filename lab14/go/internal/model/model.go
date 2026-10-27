// Пакет model — форматы данных из docs/ARCHITECTURE.md (раздел 2).
package model

import (
	"math"
	"strconv"
	"time"
)

const (
	tsLayout  = "2006-01-02T15:04:05.000Z"
	winLayout = "2006-01-02T15:04:05Z"
)

// Reading — сырое показание (2.1).
type Reading struct {
	DeviceID  string    `json:"device_id"`
	PatientID string    `json:"patient_id"`
	Seq       int64     `json:"seq"`
	Ts        time.Time `json:"ts"`
	Metric    string    `json:"metric"`
	Value     *float64  `json:"value"`
	Unit      string    `json:"unit"`
	Battery   *int      `json:"battery"`
}

// Agg — оконный агрегат (2.4). Count — число валидных значений. При Count == 0
// sum/min/max/mean/last сериализуются как null.
type Agg struct {
	WindowStart time.Time `json:"window_start"`
	WindowEnd   time.Time `json:"window_end"`
	DeviceID    string    `json:"device_id"`
	PatientID   string    `json:"patient_id"`
	Metric      string    `json:"metric"`
	Count       int64     `json:"count"`
	Invalid     int64     `json:"invalid"`
	Sum         float64   `json:"sum"`
	Min         float64   `json:"min"`
	Max         float64   `json:"max"`
	Mean        float64   `json:"mean"`
	Last        float64   `json:"last"`
	CollectorID string    `json:"collector_id"`
}

// Range — допустимый диапазон и единица метрики (раздел 2).
type Range struct {
	Lo, Hi float64
	Unit   string
}

// Metrics — порядок метрик в общей нумерации seq устройства.
var Metrics = []string{"heart_rate", "spo2", "temperature", "systolic_bp", "diastolic_bp", "resp_rate", "steps"}

var Ranges = map[string]Range{
	"heart_rate":   {25, 250, "bpm"},
	"spo2":         {50, 100, "%"},
	"temperature":  {30.0, 43.0, "C"},
	"systolic_bp":  {50, 260, "mmHg"},
	"diastolic_bp": {30, 160, "mmHg"},
	"resp_rate":    {4, 60, "breaths/min"},
	"steps":        {0, 400, "count"},
}

// ValidValue — значение не null/NaN, метрика известна, значение в диапазоне.
func ValidValue(metric string, v *float64) bool {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return false
	}
	r, ok := Ranges[metric]
	return ok && *v >= r.Lo && *v <= r.Hi
}

func (r Reading) MarshalJSON() ([]byte, error) { return r.AppendJSON(make([]byte, 0, 200)), nil }

// AppendJSON дописывает компактный JSON строки raw (порядок полей как в контракте).
func (r Reading) AppendJSON(b []byte) []byte {
	b = append(b, `{"device_id":`...)
	b = appendString(b, r.DeviceID)
	b = append(b, `,"patient_id":`...)
	b = appendString(b, r.PatientID)
	b = append(b, `,"seq":`...)
	b = strconv.AppendInt(b, r.Seq, 10)
	b = append(b, `,"ts":"`...)
	b = r.Ts.UTC().AppendFormat(b, tsLayout)
	b = append(b, `","metric":`...)
	b = appendString(b, r.Metric)
	b = append(b, `,"value":`...)
	if r.Value == nil || math.IsNaN(*r.Value) || math.IsInf(*r.Value, 0) {
		b = append(b, "null"...)
	} else {
		b = appendFloat(b, *r.Value)
	}
	b = append(b, `,"unit":`...)
	b = appendString(b, r.Unit)
	b = append(b, `,"battery":`...)
	if r.Battery == nil {
		b = append(b, "null"...)
	} else {
		b = strconv.AppendInt(b, int64(*r.Battery), 10)
	}
	return append(b, '}')
}

func (a Agg) MarshalJSON() ([]byte, error) { return a.AppendJSON(make([]byte, 0, 320)), nil }

// AppendJSON дописывает компактный JSON строки agg.
func (a Agg) AppendJSON(b []byte) []byte {
	b = append(b, `{"window_start":"`...)
	b = a.WindowStart.UTC().AppendFormat(b, winLayout)
	b = append(b, `","window_end":"`...)
	b = a.WindowEnd.UTC().AppendFormat(b, winLayout)
	b = append(b, `","device_id":`...)
	b = appendString(b, a.DeviceID)
	b = append(b, `,"patient_id":`...)
	b = appendString(b, a.PatientID)
	b = append(b, `,"metric":`...)
	b = appendString(b, a.Metric)
	b = append(b, `,"count":`...)
	b = strconv.AppendInt(b, a.Count, 10)
	b = append(b, `,"invalid":`...)
	b = strconv.AppendInt(b, a.Invalid, 10)
	for _, f := range [...]struct {
		name string
		v    float64
	}{{"sum", a.Sum}, {"min", a.Min}, {"max", a.Max}, {"mean", a.Mean}, {"last", a.Last}} {
		b = append(b, ',', '"')
		b = append(b, f.name...)
		b = append(b, '"', ':')
		if a.Count == 0 && f.name != "sum" {
			b = append(b, "null"...)
		} else {
			b = appendFloat(b, f.v)
		}
	}
	b = append(b, `,"collector_id":`...)
	b = appendString(b, a.CollectorID)
	return append(b, '}')
}

func appendFloat(b []byte, v float64) []byte {
	start := len(b)
	b = strconv.AppendFloat(b, v, 'f', -1, 64)
	for _, c := range b[start:] {
		if c == '.' {
			return b
		}
	}
	return append(b, '.', '0') // как в контракте: 72.0, а не 72
}

const hexDigits = "0123456789abcdef"

func appendString(b []byte, s string) []byte {
	b = append(b, '"')
	start := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 0x20 && c != '"' && c != '\\' {
			continue
		}
		b = append(b, s[start:i]...)
		switch c {
		case '"', '\\':
			b = append(b, '\\', c)
		case '\n':
			b = append(b, '\\', 'n')
		case '\r':
			b = append(b, '\\', 'r')
		case '\t':
			b = append(b, '\\', 't')
		default:
			b = append(b, '\\', 'u', '0', '0', hexDigits[c>>4], hexDigits[c&0xf])
		}
		start = i + 1
	}
	b = append(b, s[start:]...)
	return append(b, '"')
}
