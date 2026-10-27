// Пакет config читает настройки агента из окружения.
package config

import (
	"fmt"
	"strconv"
	"time"
)

type Config struct {
	NATSURL      string
	RedisURL     string
	RedisSet     bool // REDIS_URL задан явно (для триажа без него — хранилище в памяти)
	AgentID      string
	LogLevel     string
	LogFile      string
	ClinicTZ     string
	FailRate     float64
	Slow         time.Duration
	ReminderDemo time.Duration
	SurveyDemo   time.Duration
}

// FromEnv собирает конфигурацию; getenv инжектируется для тестов.
func FromEnv(getenv func(string) string, step string, pid int) (Config, error) {
	def := func(key, d string) string {
		if v := getenv(key); v != "" {
			return v
		}
		return d
	}
	c := Config{
		NATSURL:  def("NATS_URL", "nats://localhost:4222"),
		RedisURL: def("REDIS_URL", "redis://localhost:6379/0"),
		RedisSet: getenv("REDIS_URL") != "",
		AgentID:  def("AGENT_ID", fmt.Sprintf("%s-%d", step, pid)),
		LogLevel: def("LOG_LEVEL", "info"),
		LogFile:  getenv("LOG_FILE"),
		ClinicTZ: def("CLINIC_TZ", "Europe/Moscow"),
	}
	if v := getenv("AGENT_FAIL_RATE"); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil || f < 0 || f > 1 {
			return c, fmt.Errorf("AGENT_FAIL_RATE=%q: ожидается число от 0 до 1", v)
		}
		c.FailRate = f
	}
	if v := getenv("AGENT_SLOW_MS"); v != "" {
		ms, err := strconv.Atoi(v)
		if err != nil || ms < 0 {
			return c, fmt.Errorf("AGENT_SLOW_MS=%q: ожидается неотрицательное число миллисекунд", v)
		}
		c.Slow = time.Duration(ms) * time.Millisecond
	}
	var err error
	if c.ReminderDemo, err = parseDelay(getenv("REMINDER_DEMO_DELAY")); err != nil {
		return c, fmt.Errorf("REMINDER_DEMO_DELAY: %w", err)
	}
	if c.SurveyDemo, err = parseDelay(getenv("SURVEY_DEMO_DELAY")); err != nil {
		return c, fmt.Errorf("SURVEY_DEMO_DELAY: %w", err)
	}
	return c, nil
}

func parseDelay(v string) (time.Duration, error) {
	if v == "" {
		return 0, nil
	}
	d, err := time.ParseDuration(v)
	if err != nil || d < 0 {
		return 0, fmt.Errorf("%q: ожидается длительность вида 20s", v)
	}
	return d, nil
}
