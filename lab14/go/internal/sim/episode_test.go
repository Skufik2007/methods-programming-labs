package sim

import (
	"math"
	"testing"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

// Находка №3: на пике эпизода значение становилось ровно целевым (САД 175,0 у всех
// пациентов, min = max = mean на 200 показаниях). Живой сигнал так не выглядит, а
// одинаковая константа у разных пациентов делает эпизоды неразличимыми на графиках.
// На пике эпизода показатель должен оставаться за порогом, но сохранять дрейф и шум.
func TestEpisodePeakKeepsVariation(t *testing.T) {
	g := New(Params{Seed: 42, Devices: 200, PeriodMs: 1000, Start: time.Unix(0, 0)})
	found := 0
	for n := 0; n < 200 && found < 3; n++ {
		// ищем момент, когда эпизод пациента на полной силе
		for ms := int64(0); ms < 2*slotMs; ms += 5000 {
			kind, env := g.episode(n, ms)
			if env < 1 {
				continue
			}
			target := episodeTargets[kind]
			mi := metricIndex(t, target.metric)
			var vals []float64
			for k := int64(0); k < 30; k++ {
				vals = append(vals, g.cleanValue(n, mi, k+1, ms+k*200))
			}
			if spread(vals) == 0 {
				t.Fatalf("пациент %d, %s: на пике эпизода значения постоянны (%v)", n, target.metric, vals[0])
			}
			found++
			break
		}
	}
	if found == 0 {
		t.Fatal("не нашлось ни одного эпизода на полной силе — проверьте параметры теста")
	}
}

func metricIndex(t *testing.T, metric string) int {
	t.Helper()
	for i, m := range model.Metrics {
		if m == metric {
			return i
		}
	}
	t.Fatalf("нет метрики %s", metric)
	return -1
}

func spread(v []float64) float64 {
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, x := range v {
		lo, hi = math.Min(lo, x), math.Max(hi, x)
	}
	return hi - lo
}
