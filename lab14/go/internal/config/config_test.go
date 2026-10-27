package config

import (
	"strings"
	"testing"
	"time"
)

func env(m map[string]string) Getenv {
	return func(k string) string { return m[k] }
}

func TestLoadSimDefaults(t *testing.T) {
	c, err := LoadSim(env(nil))
	if err != nil {
		t.Fatalf("без переменных: %v", err)
	}
	if c.Devices != 100 || c.PeriodMs != 1000 || c.Addr != ":8080" {
		t.Fatalf("значения по умолчанию: %+v", c)
	}
	if c.DefectRange != 0.02 || c.DefectFuture != 0.01 {
		t.Fatalf("доли дефектов по умолчанию: %+v", c)
	}
}

func TestLoadCollectorDefaults(t *testing.T) {
	c, err := LoadCollector(env(nil))
	if err != nil {
		t.Fatalf("без переменных: %v", err)
	}
	if c.Workers != 16 || c.PollInterval != time.Second || c.Buffer != 10000 || c.BatchSize != 500 {
		t.Fatalf("значения по умолчанию: %+v", c)
	}
	if c.FlushInterval != 2*time.Second || c.Window != 10*time.Second || c.WindowLateness != 2*time.Second {
		t.Fatalf("интервалы по умолчанию: %+v", c)
	}
	if c.Emit != "both" || len(c.Sinks) != 1 || c.Sinks[0] != "files" {
		t.Fatalf("EMIT/SINKS по умолчанию: %q %v", c.Emit, c.Sinks)
	}
}

func TestLoadErrors(t *testing.T) {
	cases := []struct {
		name    string
		load    string // sim | collector
		vars    map[string]string
		wantKey string // имя переменной, которое должно быть в тексте ошибки
	}{
		{"sim: не число", "sim", map[string]string{"DEVICES": "много"}, "DEVICES"},
		{"sim: вне диапазона", "sim", map[string]string{"DEVICES": "0"}, "DEVICES"},
		{"sim: PERIOD_MS слишком мал", "sim", map[string]string{"PERIOD_MS": "1"}, "PERIOD_MS"},
		{"sim: доля больше 1", "sim", map[string]string{"DEFECT_NULL": "1.5"}, "DEFECT_NULL"},
		{"sim: доля отрицательная", "sim", map[string]string{"DEFECT_DUP": "-0.1"}, "DEFECT_DUP"},
		{"sim: START не RFC 3339", "sim", map[string]string{"START": "13.10.2026"}, "START"},
		{"sim: LATENCY_MS больше минуты", "sim", map[string]string{"LATENCY_MS": "61000"}, "LATENCY_MS"},
		{"collector: WORKERS 0", "collector", map[string]string{"WORKERS": "0"}, "WORKERS"},
		{"collector: POLL_INTERVAL некорректна", "collector", map[string]string{"POLL_INTERVAL": "быстро"}, "POLL_INTERVAL"},
		{"collector: POLL_INTERVAL меньше минимума", "collector", map[string]string{"POLL_INTERVAL": "1ms"}, "POLL_INTERVAL"},
		{"collector: WINDOW не кратно секунде", "collector", map[string]string{"WINDOW": "1500ms"}, "WINDOW"},
		{"collector: WINDOW меньше секунды", "collector", map[string]string{"WINDOW": "500ms"}, "WINDOW"},
		{"collector: EMIT неизвестен", "collector", map[string]string{"EMIT": "json"}, "EMIT"},
		{"collector: неизвестный сток", "collector", map[string]string{"SINKS": "files,kafka"}, "SINKS"},
		{"collector: пустой набор стоков", "collector", map[string]string{"SINKS": " , "}, "SINKS"},
		{"collector: COORD неизвестен", "collector", map[string]string{"COORD": "zookeeper"}, "COORD"},
		{"collector: etcd без ETCD_ENDPOINTS", "collector", map[string]string{"COORD": "etcd"}, "ETCD_ENDPOINTS"},
		{"collector: FETCH_LIMIT больше 1000", "collector", map[string]string{"FETCH_LIMIT": "5000"}, "FETCH_LIMIT"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var err error
			if c.load == "sim" {
				_, err = LoadSim(env(c.vars))
			} else {
				_, err = LoadCollector(env(c.vars))
			}
			if err == nil {
				t.Fatalf("ожидалась ошибка для %v", c.vars)
			}
			if !strings.Contains(err.Error(), c.wantKey) {
				t.Fatalf("текст ошибки не называет переменную %s: %v", c.wantKey, err)
			}
		})
	}
}

func TestEtcdEndpointsParsed(t *testing.T) {
	c, err := LoadCollector(env(map[string]string{
		"COORD": "etcd", "ETCD_ENDPOINTS": " localhost:2379 , localhost:2380 ",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.EtcdEndpoints) != 2 || c.EtcdEndpoints[0] != "localhost:2379" || c.EtcdEndpoints[1] != "localhost:2380" {
		t.Fatalf("endpoints %v", c.EtcdEndpoints)
	}
}

func TestSimStartParsedAsUTC(t *testing.T) {
	c, err := LoadSim(env(map[string]string{"START": "2026-10-13T12:30:00+03:00"}))
	if err != nil {
		t.Fatal(err)
	}
	if !c.Start.Equal(time.Date(2026, 10, 13, 9, 30, 0, 0, time.UTC)) || c.Start.Location() != time.UTC {
		t.Fatalf("START = %v", c.Start)
	}
}

func TestMultipleErrorsReported(t *testing.T) {
	_, err := LoadCollector(env(map[string]string{"WORKERS": "0", "EMIT": "x"}))
	if err == nil || !strings.Contains(err.Error(), "WORKERS") || !strings.Contains(err.Error(), "EMIT") {
		t.Fatalf("ожидались обе ошибки сразу, получено: %v", err)
	}
}
