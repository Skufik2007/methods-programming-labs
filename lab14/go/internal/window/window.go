// Пакет window — tumbling-окна по разделу 2.4. Не потокобезопасен для Add/Close*;
// Stats можно читать из любой горутины. Время приходит параметром (инжекция часов).
package window

import (
	"sort"
	"sync/atomic"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

// FutureSkew — насколько ts может опережать часы сборщика, прежде чем показание
// считается сбоем часов устройства.
const FutureSkew = time.Minute

type Result int

const (
	Accepted  Result = iota
	Late             // окно уже закрыто: late_dropped
	Duplicate        // seq уже был — в агрегат не входит
)

type winKey struct {
	device, metric string
	start          int64 // мс эпохи
}

type state struct {
	patient             string
	count, invalid      int64
	sum, min, max, last float64
}

type Stats struct {
	Open, Closed, EarlyClosed, LateDropped, DupSkipped, FutureClamped int64
}

type Aggregator struct {
	collectorID string
	sizeMs      int64
	latenessMs  int64
	open        map[winKey]*state
	lastSeq     map[string]int64

	nOpen, closed, early, late, dup, future atomic.Int64
}

func New(collectorID string, size, lateness time.Duration) *Aggregator {
	return &Aggregator{
		collectorID: collectorID,
		sizeMs:      size.Milliseconds(),
		latenessMs:  lateness.Milliseconds(),
		open:        make(map[winKey]*state),
		lastSeq:     make(map[string]int64),
	}
}

func floorMod(a, m int64) int64 { return ((a % m) + m) % m }

// Add относит показание к окну по ts. now — время сборщика.
func (a *Aggregator) Add(r model.Reading, now time.Time) Result {
	if r.Seq <= a.lastSeq[r.DeviceID] {
		a.dup.Add(1)
		return Duplicate
	}
	a.lastSeq[r.DeviceID] = r.Seq

	nowMs := now.UnixMilli()
	ts := r.Ts.UnixMilli()
	forceInvalid := false
	if ts > nowMs+FutureSkew.Milliseconds() {
		// ts из будущего: считаем в окне текущего времени сборщика как невалидное
		ts = nowMs
		forceInvalid = true
		a.future.Add(1)
	}
	start := ts - floorMod(ts, a.sizeMs)
	if nowMs >= start+a.sizeMs+a.latenessMs {
		a.late.Add(1)
		return Late
	}
	k := winKey{r.DeviceID, r.Metric, start}
	st := a.open[k]
	if st == nil {
		st = &state{patient: r.PatientID}
		a.open[k] = st
		a.nOpen.Add(1)
	}
	if forceInvalid || !model.ValidValue(r.Metric, r.Value) {
		st.invalid++
		return Accepted
	}
	v := *r.Value
	if st.count == 0 {
		st.min, st.max = v, v
	} else {
		st.min = min(st.min, v)
		st.max = max(st.max, v)
	}
	st.count++
	st.sum += v
	st.last = v // «последнее» — по порядку поступления
	return Accepted
}

// CloseDue закрывает окна, у которых now >= window_end + lateness.
func (a *Aggregator) CloseDue(now time.Time) []model.Agg {
	return a.closeWhere(now, func(k winKey) bool {
		return now.UnixMilli() >= k.start+a.sizeMs+a.latenessMs
	}, false)
}

// CloseAll закрывает все открытые окна (graceful shutdown); те, что ещё не
// дожили до срока, считаются закрытыми досрочно.
func (a *Aggregator) CloseAll(now time.Time) []model.Agg {
	return a.closeWhere(now, func(winKey) bool { return true }, true)
}

func (a *Aggregator) closeWhere(now time.Time, pred func(winKey) bool, countEarly bool) []model.Agg {
	var out []model.Agg
	nowMs := now.UnixMilli()
	for k, st := range a.open {
		if !pred(k) {
			continue
		}
		if countEarly && nowMs < k.start+a.sizeMs+a.latenessMs {
			a.early.Add(1)
		}
		g := model.Agg{
			WindowStart: time.UnixMilli(k.start).UTC(),
			WindowEnd:   time.UnixMilli(k.start + a.sizeMs).UTC(),
			DeviceID:    k.device, PatientID: st.patient, Metric: k.metric,
			Count: st.count, Invalid: st.invalid, CollectorID: a.collectorID,
		}
		if st.count > 0 {
			g.Sum, g.Min, g.Max, g.Last = st.sum, st.min, st.max, st.last
			g.Mean = st.sum / float64(st.count)
		}
		out = append(out, g)
		delete(a.open, k)
	}
	a.nOpen.Add(-int64(len(out)))
	a.closed.Add(int64(len(out)))
	sort.Slice(out, func(i, j int) bool {
		x, y := out[i], out[j]
		if !x.WindowStart.Equal(y.WindowStart) {
			return x.WindowStart.Before(y.WindowStart)
		}
		if x.DeviceID != y.DeviceID {
			return x.DeviceID < y.DeviceID
		}
		return x.Metric < y.Metric
	})
	return out
}

func (a *Aggregator) Stats() Stats {
	return Stats{
		Open: a.nOpen.Load(), Closed: a.closed.Load(), EarlyClosed: a.early.Load(),
		LateDropped: a.late.Load(), DupSkipped: a.dup.Load(), FutureClamped: a.future.Load(),
	}
}
