package config

import (
	"testing"
	"time"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestFromEnvDefaults(t *testing.T) {
	t.Parallel()
	c, err := FromEnv(env(nil), "triage", 42)
	if err != nil {
		t.Fatal(err)
	}
	if c.NATSURL != "nats://localhost:4222" || c.RedisURL != "redis://localhost:6379/0" || c.RedisSet {
		t.Errorf("адреса по умолчанию: %+v", c)
	}
	if c.AgentID != "triage-42" || c.LogLevel != "info" || c.ClinicTZ != "Europe/Moscow" {
		t.Errorf("значения по умолчанию: %+v", c)
	}
}

func TestFromEnvOverrides(t *testing.T) {
	t.Parallel()
	c, err := FromEnv(env(map[string]string{
		"NATS_URL": "nats://n:4222", "REDIS_URL": "redis://r:6379/1", "AGENT_ID": "booking-1",
		"LOG_LEVEL": "debug", "LOG_FILE": "/tmp/x.log", "CLINIC_TZ": "UTC",
		"AGENT_FAIL_RATE": "0.25", "AGENT_SLOW_MS": "150",
		"REMINDER_DEMO_DELAY": "20s", "SURVEY_DEMO_DELAY": "1m",
	}), "booking", 1)
	if err != nil {
		t.Fatal(err)
	}
	if c.NATSURL != "nats://n:4222" || !c.RedisSet || c.AgentID != "booking-1" || c.ClinicTZ != "UTC" {
		t.Errorf("переопределения: %+v", c)
	}
	if c.FailRate != 0.25 || c.Slow != 150*time.Millisecond {
		t.Errorf("сбои и задержка: %v %v", c.FailRate, c.Slow)
	}
	if c.ReminderDemo != 20*time.Second || c.SurveyDemo != time.Minute {
		t.Errorf("демо-задержки: %v %v", c.ReminderDemo, c.SurveyDemo)
	}
}

func TestFromEnvErrors(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		env  map[string]string
	}{
		{"доля сбоев больше 1", map[string]string{"AGENT_FAIL_RATE": "1.5"}},
		{"доля сбоев не число", map[string]string{"AGENT_FAIL_RATE": "много"}},
		{"доля сбоев отрицательная", map[string]string{"AGENT_FAIL_RATE": "-0.1"}},
		{"отрицательная задержка", map[string]string{"AGENT_SLOW_MS": "-5"}},
		{"задержка не число", map[string]string{"AGENT_SLOW_MS": "x"}},
		{"демо-задержка напоминаний", map[string]string{"REMINDER_DEMO_DELAY": "потом"}},
		{"отрицательная демо-задержка опросов", map[string]string{"SURVEY_DEMO_DELAY": "-1s"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if _, err := FromEnv(env(c.env), "triage", 1); err == nil {
				t.Fatal("ожидалась ошибка")
			}
		})
	}
}
