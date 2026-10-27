package sim

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

var testStart = time.Date(2026, 10, 13, 9, 0, 0, 0, time.UTC)

func allDefects(p float64) Defects {
	return Defects{Range: p, Null: p, Dup: p, Unit: p, Future: p}
}

func TestDeterminism(t *testing.T) {
	cases := []struct {
		name     string
		seedA    int64
		seedB    int64
		wantSame bool
	}{
		{"same seed", 7, 7, true},
		{"other seed", 7, 8, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := Params{Devices: 5, PeriodMs: 1000, Start: testStart, Defects: allDefects(0.02)}
			pa, pb := p, p
			pa.Seed, pb.Seed = tc.seedA, tc.seedB
			ga, gb := New(pa), New(pb)
			now := testStart.Add(2 * time.Minute)
			same := true
			for n := 1; n <= 5; n++ {
				ra, _, _ := ga.Readings(n, 0, 200, now)
				rb, _, _ := gb.Readings(n, 0, 200, now)
				if !reflect.DeepEqual(ra, rb) {
					same = false
				}
			}
			if same != tc.wantSame {
				t.Fatalf("совпадение показаний = %v, ожидалось %v", same, tc.wantSame)
			}
		})
	}
}

// Одно и то же k при повторном вычислении даёт тот же результат (нет состояния).
func TestReadingIsPureFunction(t *testing.T) {
	g := New(Params{Seed: 3, Devices: 10, PeriodMs: 1000, Start: testStart, Defects: allDefects(0.03)})
	for _, k := range []int64{1, 2, 7, 500, 9999} {
		a, b := g.Reading(4, k), g.Reading(4, k)
		if !reflect.DeepEqual(a, b) {
			t.Fatalf("k=%d: повторный вызов дал другое показание", k)
		}
	}
}

func TestSeqMonotonicAcrossPages(t *testing.T) {
	g := New(Params{Seed: 11, Devices: 3, PeriodMs: 1000, Start: testStart, Defects: allDefects(0.05)})
	now := testStart.Add(90 * time.Second)
	var seqs []int64
	after := int64(0)
	for {
		rs, last, _ := g.Readings(2, after, 7, now)
		if len(rs) == 0 {
			break
		}
		for _, r := range rs {
			seqs = append(seqs, r.Seq)
		}
		after = last
	}
	if len(seqs) == 0 {
		t.Fatal("нет показаний")
	}
	for i := 1; i < len(seqs); i++ {
		d := seqs[i] - seqs[i-1]
		// шаг 1 либо повтор того же seq (дубликат, допустим не более одного подряд)
		if d != 0 && d != 1 {
			t.Fatalf("разрыв seq: %d -> %d", seqs[i-1], seqs[i])
		}
	}
	if seqs[0] != 1 {
		t.Fatalf("seq должен начинаться с 1, получено %d", seqs[0])
	}
}

func TestDefectRatesWithinTolerance(t *testing.T) {
	const want = 0.02
	g := New(Params{Seed: 99, Devices: 200, PeriodMs: 1000, Start: testStart, Defects: allDefects(want)})
	var total, rng, null, unit, future int
	for n := 1; n <= 200; n++ {
		for k := int64(1); k <= 100; k++ {
			r := g.Reading(n, k)
			total++
			if r.Value == nil {
				null++
			} else if !model.ValidValue(r.Metric, r.Value) {
				rng++
			}
			if !strings.EqualFold(r.Unit, model.Ranges[r.Metric].Unit) || r.Unit == "" {
				unit++
			}
			if r.Ts.After(testStart.Add(time.Hour)) {
				future++
			}
		}
	}
	if total != 20000 {
		t.Fatalf("всего показаний %d", total)
	}
	expect := want * float64(total)
	cases := []struct {
		name string
		got  int
		lo   float64
		hi   float64
	}{
		{"null", null, 0.5 * expect, 1.5 * expect},
		{"range", rng, 0.4 * expect, 1.2 * expect},
		{"unit", unit, 0.4 * expect, 1.6 * expect},
		{"future", future, 0.4 * expect, 1.6 * expect},
	}
	for _, c := range cases {
		if float64(c.got) < c.lo || float64(c.got) > c.hi {
			t.Errorf("%s: %d вне допуска [%.0f, %.0f] (ожидалось ~%.0f)", c.name, c.got, c.lo, c.hi, expect)
		}
	}
}

func TestHTTPReadingsAfterSeqAndLimit(t *testing.T) {
	g := New(Params{Seed: 5, Devices: 4, PeriodMs: 1000, Start: testStart})
	clk := clock.NewFake(testStart.Add(30 * time.Second))
	ts := httptest.NewServer(NewServer(g, clk, 0, 0).Handler())
	defer ts.Close()

	get := func(path string) (int, []byte) {
		t.Helper()
		resp, err := http.Get(ts.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, b
	}

	type rdResp struct {
		DeviceID string          `json:"device_id"`
		Readings []model.Reading `json:"readings"`
		LastSeq  int64           `json:"last_seq"`
	}

	t.Run("limit caps batch", func(t *testing.T) {
		code, b := get("/v1/devices/dev-0001/readings?after_seq=0&limit=5")
		if code != 200 {
			t.Fatalf("код %d", code)
		}
		var r rdResp
		if err := json.Unmarshal(b, &r); err != nil {
			t.Fatal(err)
		}
		if len(r.Readings) != 5 || r.LastSeq != r.Readings[4].Seq {
			t.Fatalf("получено %d показаний, last_seq=%d", len(r.Readings), r.LastSeq)
		}
		if r.Readings[0].Seq != 1 {
			t.Fatalf("первый seq %d, ожидался 1", r.Readings[0].Seq)
		}
	})

	t.Run("after_seq continues", func(t *testing.T) {
		_, b := get("/v1/devices/dev-0001/readings?after_seq=5&limit=3")
		var r rdResp
		_ = json.Unmarshal(b, &r)
		if len(r.Readings) == 0 || r.Readings[0].Seq <= 5 {
			t.Fatalf("после after_seq=5 пришли %v", r.Readings)
		}
	})

	t.Run("limit above 1000 clamped", func(t *testing.T) {
		_, b := get("/v1/devices/dev-0001/readings?after_seq=0&limit=5000")
		var r rdResp
		_ = json.Unmarshal(b, &r)
		if len(r.Readings) > 1000 {
			t.Fatalf("limit не ограничен: %d", len(r.Readings))
		}
	})

	badCases := []struct {
		name, path string
		code       int
	}{
		{"bad limit", "/v1/devices/dev-0001/readings?limit=0", 400},
		{"bad after_seq", "/v1/devices/dev-0001/readings?after_seq=-1", 400},
		{"unknown device", "/v1/devices/dev-9999/readings", 404},
	}
	for _, c := range badCases {
		t.Run(c.name, func(t *testing.T) {
			if code, _ := get(c.path); code != c.code {
				t.Fatalf("код %d, ожидался %d", code, c.code)
			}
		})
	}

	t.Run("devices and healthz", func(t *testing.T) {
		code, b := get("/v1/devices")
		if code != 200 || !strings.Contains(string(b), `"dev-0004"`) {
			t.Fatalf("devices: код %d", code)
		}
		if code, _ := get("/healthz"); code != 200 {
			t.Fatalf("healthz %d", code)
		}
	})
}
