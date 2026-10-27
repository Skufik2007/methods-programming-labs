// Пакет sim — детерминированный генератор показаний: показание номер k устройства
// n — чистая функция (SEED, n, k). Состояния нет: «случившиеся» показания
// вычисляются по текущему времени.
package sim

import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

type Defects struct{ Range, Null, Dup, Unit, Future float64 }

type Params struct {
	Seed     int64
	Devices  int
	PeriodMs int64
	Start    time.Time
	Defects  Defects
}

type Generator struct {
	p         Params
	startMs   int64
	nMetrics  int64
	restRange map[string][2]float64
}

// соли независимых потоков хэша
const (
	saltBase uint64 = iota + 1
	saltPhase1
	saltPhase2
	saltPeriod1
	saltPeriod2
	saltNoise
	saltDevPhase
	saltBattery
	saltBatteryNull
	saltPatientProne
	saltSlotHas
	saltSlotDur
	saltSlotOff
	saltSlotKind
	saltDRange
	saltDNull
	saltDDup
	saltDUnit
	saltDFuture
)

func splitmix(x uint64) uint64 {
	x += 0x9e3779b97f4a7c15
	x = (x ^ x>>30) * 0xbf58476d1ce4e5b9
	x = (x ^ x>>27) * 0x94d049bb133111eb
	return x ^ x>>31
}

func (g *Generator) hash(a, b, c uint64) uint64 {
	h := splitmix(uint64(g.p.Seed))
	h = splitmix(h ^ a)
	h = splitmix(h ^ b)
	return splitmix(h ^ c)
}

// u — равномерное число в [0, 1) из хэша.
func (g *Generator) u(a, b, c uint64) float64 { return float64(g.hash(a, b, c)>>11) / (1 << 53) }

func New(p Params) *Generator {
	return &Generator{
		p: p, startMs: p.Start.UnixMilli(), nMetrics: int64(len(model.Metrics)),
		// «норма покоя» из раздела 2
		restRange: map[string][2]float64{
			"heart_rate": {60, 90}, "spo2": {95, 99}, "temperature": {36.3, 37.0},
			"systolic_bp": {105, 135}, "diastolic_bp": {65, 85}, "resp_rate": {12, 18}, "steps": {0, 30},
		},
	}
}

func (g *Generator) Devices() int { return g.p.Devices }

func (g *Generator) PeriodMs() int64 { return g.p.PeriodMs }

// DeviceID: n = 1..Devices.
func (g *Generator) DeviceID(n int) string  { return fmt.Sprintf("dev-%04d", n) }
func (g *Generator) PatientID(n int) string { return fmt.Sprintf("p-%04d", n) }

func (g *Generator) DeviceIndex(id string) (int, bool) {
	s, ok := strings.CutPrefix(id, "dev-")
	if !ok {
		return 0, false
	}
	n, err := strconv.Atoi(s)
	if err != nil || n < 1 || n > g.p.Devices || g.DeviceID(n) != id {
		return 0, false
	}
	return n, true
}

// phase — сдвиг устройства внутри периода, чтобы устройства не стреляли синхронно.
func (g *Generator) phase(n int) int64 {
	return int64(g.hash(saltDevPhase, uint64(n), 0) % uint64(g.p.PeriodMs))
}

// timeMs — момент (мс эпохи) появления показания k. Каждый период каждое устройство
// выдаёт по одному показанию каждой метрики, равномерно по периоду.
func (g *Generator) timeMs(n int, k int64) int64 {
	c, m := (k-1)/g.nMetrics, (k-1)%g.nMetrics
	return g.startMs + g.phase(n) + c*g.p.PeriodMs + m*g.p.PeriodMs/g.nMetrics
}

// Count — сколько показаний устройства уже случилось к now.
func (g *Generator) Count(n int, now time.Time) int64 {
	el := now.UnixMilli() - g.startMs - g.phase(n)
	if el < 0 {
		return 0
	}
	cnt := (el / g.p.PeriodMs) * g.nMetrics
	rem := el % g.p.PeriodMs
	for m := int64(0); m < g.nMetrics; m++ {
		if m*g.p.PeriodMs/g.nMetrics <= rem {
			cnt++
		}
	}
	return cnt
}

func smoothstep(x float64) float64 {
	x = max(0, min(1, x))
	return x * x * (3 - 2*x)
}

var episodeTargets = [4]struct {
	metric string
	target float64
}{
	{"heart_rate", 140},   // тахикардия > 120
	{"spo2", 86},          // десатурация < 92
	{"temperature", 38.7}, // лихорадка > 38.0
	{"systolic_bp", 175},  // гипертония > 160
}

const (
	slotMs     = 20 * 60 * 1000
	rampMs     = 10 * 1000
	proneShare = 0.35 // доля пациентов, у которых бывают эпизоды
	slotShare  = 0.60 // вероятность эпизода в 20-минутном слоте
)

// episode возвращает (индекс типа, огибающая 0..1) для пациента в момент t.
func (g *Generator) episode(n int, t int64) (kind int, env float64) {
	if g.u(saltPatientProne, uint64(n), 0) >= proneShare {
		return 0, 0
	}
	slot := t / slotMs
	if g.u(saltSlotHas, uint64(n), uint64(slot)) >= slotShare {
		return 0, 0
	}
	dur := int64(60_000 + g.u(saltSlotDur, uint64(n), uint64(slot))*9*60_000) // 1..10 мин
	off := int64(g.u(saltSlotOff, uint64(n), uint64(slot)) * float64(slotMs-dur))
	st := slot*slotMs + off
	if t < st || t >= st+dur {
		return 0, 0
	}
	env = min(smoothstep(float64(t-st)/rampMs), smoothstep(float64(st+dur-t)/rampMs))
	return int(g.hash(saltSlotKind, uint64(n), uint64(slot)) % 4), env
}

var decimals = map[string]int{"heart_rate": 0, "spo2": 1, "temperature": 2, "systolic_bp": 0, "diastolic_bp": 0, "resp_rate": 0, "steps": 0}

var badValues = map[string]float64{
	"heart_rate": 999, "spo2": -1, "temperature": 99.9, "systolic_bp": 999,
	"diastolic_bp": -5, "resp_rate": 300, "steps": 99999,
}

// cleanValue — «здоровое» значение: базовый уровень пациента + плавный дрейф
// (две синусоиды с периодами минуты..минуты) + малый шум, затем эпизод.
func (g *Generator) cleanValue(n int, mi int, k int64, t int64) float64 {
	metric := model.Metrics[mi]
	lo, hi := g.restRange[metric][0], g.restRange[metric][1]
	w := hi - lo
	idx := uint64(mi)
	base := lo + w*g.u(saltBase, uint64(n), idx)
	sec := float64(t) / 1000
	p1 := 240 + 360*g.u(saltPeriod1, uint64(n), idx)
	p2 := 30 + 40*g.u(saltPeriod2, uint64(n), idx)
	f1 := 2 * math.Pi * g.u(saltPhase1, uint64(n), idx)
	f2 := 2 * math.Pi * g.u(saltPhase2, uint64(n), idx)
	v := base + 0.30*w*math.Sin(2*math.Pi*sec/p1+f1) + 0.10*w*math.Sin(2*math.Pi*sec/p2+f2)
	v += (g.u(saltNoise, uint64(n), uint64(k)) - 0.5) * 0.06 * w
	if kind, env := g.episode(n, t); env > 0 && episodeTargets[kind].metric == metric {
		// Сдвигаем базовый уровень к целевому, сохраняя дрейф и шум: смешивание
		// v·(1−env)+target·env на пике давало ровно target — плоскую линию, одинаковую
		// у всех пациентов (находка №3).
		v += (episodeTargets[kind].target - base) * env
	}
	r := model.Ranges[metric]
	v = max(r.Lo, min(r.Hi, v))
	p := math.Pow(10, float64(decimals[metric]))
	return math.Round(v*p) / p
}

// Reading строит показание с номером k (k >= 1) устройства n вместе с дефектами.
func (g *Generator) Reading(n int, k int64) model.Reading {
	mi := int((k - 1) % g.nMetrics)
	metric := model.Metrics[mi]
	t := g.timeMs(n, k)
	un, uk := uint64(n), uint64(k)

	val := g.cleanValue(n, mi, k, t)
	unit := model.Ranges[metric].Unit
	ts := t

	// заряд убывает ~1% за 6 мин и «подзаряжается» по достижении 0
	bat := 100 - int(((t/60_000)+int64(g.hash(saltBattery, un, 0)%600))%600)/6
	bat = max(0, min(100, bat))
	var battery *int
	if g.u(saltBatteryNull, un, uk) >= 0.01 {
		battery = &bat
	}

	d := g.p.Defects
	var value *float64 = &val
	if g.u(saltDRange, un, uk) < d.Range {
		bad := badValues[metric]
		value = &bad
	}
	if g.u(saltDNull, un, uk) < d.Null {
		value = nil
	}
	if g.u(saltDUnit, un, uk) < d.Unit {
		up := strings.ToUpper(unit)
		if g.hash(saltDUnit, un, uk)&1 == 0 || up == unit {
			unit = ""
		} else {
			unit = up
		}
	}
	if g.u(saltDFuture, un, uk) < d.Future {
		ts += time.Hour.Milliseconds()
	}
	return model.Reading{
		DeviceID: g.DeviceID(n), PatientID: g.PatientID(n), Seq: k,
		Ts: time.UnixMilli(ts).UTC(), Metric: metric, Value: value, Unit: unit, Battery: battery,
	}
}

// IsDup — будет ли показание k повторно выдано в следующем ответе.
func (g *Generator) IsDup(n int, k int64) bool {
	return k >= 1 && g.u(saltDDup, uint64(n), uint64(k)) < g.p.Defects.Dup
}

// Readings отдаёт до limit показаний с seq > after, случившихся к now. Если
// клиент уже получил показание after и оно помечено дубликатом, оно выдаётся ещё
// раз первым (дубликат не входит в limit). Второе значение — last_seq.
func (g *Generator) Readings(n int, after int64, limit int, now time.Time) (out []model.Reading, last int64, dups int) {
	after = max(after, 0)
	total := g.Count(n, now)
	last = after
	if after >= total {
		return []model.Reading{}, last, 0
	}
	to := min(total, after+int64(limit))
	out = make([]model.Reading, 0, to-after+1)
	if g.IsDup(n, after) {
		out = append(out, g.Reading(n, after))
		dups = 1
	}
	for k := after + 1; k <= to; k++ {
		out = append(out, g.Reading(n, k))
	}
	return out, to, dups
}
