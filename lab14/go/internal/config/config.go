// Пакет config — разбор переменных окружения эмулятора и сборщика с проверкой значений.
package config

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Getenv — функция чтения окружения (os.Getenv или подставная в тестах).
type Getenv func(string) string

type reader struct {
	get  Getenv
	errs []error
}

func (r *reader) fail(name, format string, a ...any) {
	r.errs = append(r.errs, fmt.Errorf("%s: %s", name, fmt.Sprintf(format, a...)))
}

func (r *reader) str(name, def string) string {
	if v := strings.TrimSpace(r.get(name)); v != "" {
		return v
	}
	return def
}

func (r *reader) integer(name string, def, lo, hi int64) int64 {
	s := strings.TrimSpace(r.get(name))
	if s == "" {
		return def
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		r.fail(name, "ожидается целое число, получено %q", s)
		return def
	}
	if v < lo || v > hi {
		r.fail(name, "значение %d вне диапазона [%d, %d]", v, lo, hi)
		return def
	}
	return v
}

func (r *reader) fraction(name string, def float64) float64 {
	s := strings.TrimSpace(r.get(name))
	if s == "" {
		return def
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || v < 0 || v > 1 {
		r.fail(name, "ожидается доля от 0 до 1, получено %q", s)
		return def
	}
	return v
}

func (r *reader) duration(name string, def, lo time.Duration) time.Duration {
	s := strings.TrimSpace(r.get(name))
	if s == "" {
		return def
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		r.fail(name, "ожидается длительность вида 500ms, 2s, 1m; получено %q", s)
		return def
	}
	if d < lo {
		r.fail(name, "значение %s меньше минимума %s", d, lo)
		return def
	}
	return d
}

func (r *reader) err() error { return errors.Join(r.errs...) }

// Sim — настройки эмулятора датчиков.
type Sim struct {
	Addr             string
	Devices          int
	PeriodMs         int64
	Seed             int64
	LatencyMs        int64
	LatencyJitterPct int64 // разброс задержки ±N% от LATENCY_MS
	DefectRange      float64
	DefectNull       float64
	DefectDup        float64
	DefectUnit       float64
	DefectFuture     float64
	Start            time.Time // нулевое значение — «старт процесса», подставляет вызывающий
}

func LoadSim(get Getenv) (Sim, error) {
	r := &reader{get: get}
	c := Sim{
		Addr:             r.str("ADDR", ":8080"),
		Devices:          int(r.integer("DEVICES", 100, 1, 1_000_000)),
		PeriodMs:         r.integer("PERIOD_MS", 1000, 10, 3_600_000),
		Seed:             r.integer("SEED", 1, -1<<62, 1<<62),
		LatencyMs:        r.integer("LATENCY_MS", 0, 0, 60_000),
		LatencyJitterPct: r.integer("LATENCY_JITTER_PCT", 25, 0, 100),
		DefectRange:      r.fraction("DEFECT_RANGE", 0.02),
		DefectNull:       r.fraction("DEFECT_NULL", 0.02),
		DefectDup:        r.fraction("DEFECT_DUP", 0.02),
		DefectUnit:       r.fraction("DEFECT_UNIT", 0.02),
		DefectFuture:     r.fraction("DEFECT_FUTURE", 0.01),
	}
	if s := strings.TrimSpace(get("START")); s != "" {
		t, err := time.Parse(time.RFC3339, s)
		if err != nil {
			r.fail("START", "ожидается время RFC 3339, получено %q", s)
		}
		c.Start = t.UTC()
	}
	return c, r.err()
}

// Collector — настройки сборщика.
type Collector struct {
	CollectorID     string
	SimURL          string
	DeviceList      []string // пусто — все устройства эмулятора
	Workers         int
	PollInterval    time.Duration
	FetchLimit      int
	Buffer          int
	BatchSize       int
	FlushInterval   time.Duration
	Window          time.Duration
	WindowLateness  time.Duration
	Emit            string // raw | agg | both
	Sinks           []string
	OutDir          string
	StatusAddr      string
	HTTPTimeout     time.Duration
	ShutdownTimeout time.Duration

	// Координация (раздел 5): static — прежние статические реализации, etcd — HRW + чекпоинты в etcd.
	Coord         string
	EtcdEndpoints []string
	EtcdTTL       time.Duration

	// NATS JetStream (сток nats).
	NatsURL string

	// Arrow Flight (сток flight).
	FlightAddr      string // адрес прослушивания gRPC
	FlightAdvertise string // адрес для публикации в etcd (пусто — вывести из FlightAddr)
	FlightRetention time.Duration
}

var knownSinks = map[string]bool{"files": true, "nats": true, "flight": true}

func LoadCollector(get Getenv) (Collector, error) {
	r := &reader{get: get}
	c := Collector{
		CollectorID:     r.str("COLLECTOR_ID", "collector-1"),
		SimURL:          strings.TrimRight(r.str("SIM_URL", "http://localhost:8080"), "/"),
		Workers:         int(r.integer("WORKERS", 16, 1, 4096)),
		PollInterval:    r.duration("POLL_INTERVAL", time.Second, 10*time.Millisecond),
		FetchLimit:      int(r.integer("FETCH_LIMIT", 1000, 1, 1000)),
		Buffer:          int(r.integer("BUFFER", 10000, 1, 100_000_000)),
		BatchSize:       int(r.integer("BATCH_SIZE", 500, 1, 10_000_000)),
		FlushInterval:   r.duration("FLUSH_INTERVAL", 2*time.Second, time.Millisecond),
		Window:          r.duration("WINDOW", 10*time.Second, time.Second),
		WindowLateness:  r.duration("WINDOW_LATENESS", 2*time.Second, 0),
		Emit:            r.str("EMIT", "both"),
		OutDir:          r.str("OUT_DIR", "data"),
		StatusAddr:      r.str("STATUS_ADDR", ":9100"),
		HTTPTimeout:     r.duration("HTTP_TIMEOUT", 10*time.Second, 100*time.Millisecond),
		ShutdownTimeout: r.duration("SHUTDOWN_TIMEOUT", 15*time.Second, time.Second),
		Coord:           r.str("COORD", "static"),
		EtcdTTL:         r.duration("ETCD_TTL", 10*time.Second, time.Second),
		NatsURL:         r.str("NATS_URL", "nats://localhost:4222"),
		FlightAddr:      r.str("FLIGHT_ADDR", ":8815"),
		FlightAdvertise: r.str("FLIGHT_ADVERTISE", ""),
		FlightRetention: r.duration("FLIGHT_RETENTION", 15*time.Minute, time.Second),
	}
	if c.Window%time.Second != 0 {
		r.fail("WINDOW", "должно быть кратно секунде (формат window_start без долей), получено %s", c.Window)
	}
	if c.WindowLateness%time.Millisecond != 0 {
		r.fail("WINDOW_LATENESS", "должно быть кратно миллисекунде, получено %s", c.WindowLateness)
	}
	switch c.Emit {
	case "raw", "agg", "both":
	default:
		r.fail("EMIT", "допустимо raw, agg или both; получено %q", c.Emit)
	}
	for _, s := range strings.Split(r.str("SINKS", "files"), ",") {
		if s = strings.TrimSpace(s); s == "" {
			continue
		}
		if !knownSinks[s] {
			r.fail("SINKS", "неизвестный сток %q (files, nats, flight)", s)
			continue
		}
		c.Sinks = append(c.Sinks, s)
	}
	if len(c.Sinks) == 0 {
		r.fail("SINKS", "нужен хотя бы один сток")
	}
	switch c.Coord {
	case "static":
	case "etcd":
		for _, e := range strings.Split(get("ETCD_ENDPOINTS"), ",") {
			if e = strings.TrimSpace(e); e != "" {
				c.EtcdEndpoints = append(c.EtcdEndpoints, e)
			}
		}
		if len(c.EtcdEndpoints) == 0 {
			r.fail("ETCD_ENDPOINTS", "обязателен при COORD=etcd (например localhost:2379)")
		}
	default:
		r.fail("COORD", "допустимо static или etcd; получено %q", c.Coord)
	}
	for _, d := range strings.Split(get("DEVICE_LIST"), ",") {
		if d = strings.TrimSpace(d); d != "" {
			c.DeviceList = append(c.DeviceList, d)
		}
	}
	return c, r.err()
}
