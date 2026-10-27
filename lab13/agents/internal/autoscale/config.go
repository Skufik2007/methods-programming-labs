package autoscale

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/registry"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/role"
)

// StepConfig — управляемый шаг.
type StepConfig struct {
	Step          string        `yaml:"step"`           // если пуст — берётся из файла роли
	Role          string        `yaml:"role"`           // путь к файлу роли (для exec — на хосте)
	ContainerRole string        `yaml:"container_role"` // путь к роли внутри контейнера (docker); по умолчанию role
	PerInstance   int           `yaml:"per_instance"`
	Min           int           `yaml:"min"`
	Max           int           `yaml:"max"`
	Cooldown      time.Duration `yaml:"cooldown"`
}

func (s StepConfig) Params() Params {
	return Params{PerInstance: s.PerInstance, Min: s.Min, Max: s.Max, Cooldown: s.Cooldown}
}

type ExecConfig struct {
	AgentBin    string        `yaml:"agent_bin"`    // путь к бинарнику агента
	StopTimeout time.Duration `yaml:"stop_timeout"` // сколько ждать мягкой остановки до Kill
}

type DockerConfig struct {
	Socket      string            `yaml:"socket"` // по умолчанию /var/run/docker.sock
	Image       string            `yaml:"image"`
	Network     string            `yaml:"network"`
	AgentBin    string            `yaml:"agent_bin"` // команда агента в образе, по умолчанию /app/agent
	Env         map[string]string `yaml:"env"`
	Binds       []string          `yaml:"binds"` // тома вида host:container[:ro]
	StopTimeout time.Duration     `yaml:"stop_timeout"`
}

type Config struct {
	Backend    string        `yaml:"backend"` // exec | docker
	Interval   time.Duration `yaml:"interval"`
	StatusPort int           `yaml:"status_port"`
	NATSURL    string        `yaml:"nats_url"` // если пусто — NATS_URL из окружения
	Exec       ExecConfig    `yaml:"exec"`
	Docker     DockerConfig  `yaml:"docker"`
	Steps      []StepConfig  `yaml:"steps"`
}

// LoadConfig читает и проверяет YAML-конфигурацию, подставляет значения по умолчанию.
func LoadConfig(path string, getenv func(string) string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("чтение конфигурации: %w", err)
	}
	var c Config
	if err := yaml.Unmarshal(data, &c); err != nil {
		return c, fmt.Errorf("разбор %s: %w", path, err)
	}
	return c, c.normalize(getenv)
}

func (c *Config) normalize(getenv func(string) string) error {
	if c.Backend == "" {
		c.Backend = "exec"
	}
	if c.Backend != "exec" && c.Backend != "docker" {
		return fmt.Errorf("backend=%q: допустимо exec или docker", c.Backend)
	}
	if c.Interval <= 0 {
		c.Interval = 2 * time.Second
	}
	if c.StatusPort == 0 {
		c.StatusPort = 8090
	}
	if c.NATSURL == "" {
		c.NATSURL = getenv("NATS_URL")
	}
	if c.NATSURL == "" {
		c.NATSURL = "nats://localhost:4222"
	}
	// Агент успевает дорабатывать задачи до 10 с (bus.DrainTimeout).
	if c.Exec.StopTimeout <= 0 {
		c.Exec.StopTimeout = 15 * time.Second
	}
	if c.Exec.AgentBin == "" {
		c.Exec.AgentBin = "agent"
	}
	if c.Docker.StopTimeout <= 0 {
		c.Docker.StopTimeout = 15 * time.Second
	}
	if c.Docker.Socket == "" {
		c.Docker.Socket = "/var/run/docker.sock"
	}
	if c.Docker.AgentBin == "" {
		c.Docker.AgentBin = "/app/agent"
	}
	if c.Backend == "docker" && c.Docker.Image == "" {
		return fmt.Errorf("docker.image не задан")
	}
	if len(c.Steps) == 0 {
		return fmt.Errorf("не задан ни один шаг (steps)")
	}
	seen := map[string]bool{}
	for i := range c.Steps {
		s := &c.Steps[i]
		if s.Step == "" {
			if s.Role == "" {
				return fmt.Errorf("steps[%d]: нужен step или role", i)
			}
			src, err := os.ReadFile(s.Role)
			if err != nil {
				return fmt.Errorf("steps[%d]: имя шага не задано, а роль не прочитать: %w", i, err)
			}
			r, err := role.Parse(string(src), registry.Kinds())
			if err != nil {
				return fmt.Errorf("steps[%d]: роль %s: %w", i, s.Role, err)
			}
			s.Step = r.Step
		}
		if seen[s.Step] {
			return fmt.Errorf("шаг %q указан дважды", s.Step)
		}
		seen[s.Step] = true
		if s.Role == "" {
			return fmt.Errorf("шаг %s: не задан role", s.Step)
		}
		if s.ContainerRole == "" {
			s.ContainerRole = s.Role
		}
		if s.PerInstance < 1 {
			return fmt.Errorf("шаг %s: per_instance должен быть >= 1", s.Step)
		}
		if s.Min < 0 {
			return fmt.Errorf("шаг %s: min < 0", s.Step)
		}
		if s.Max < 1 || s.Max < s.Min {
			return fmt.Errorf("шаг %s: max должен быть >= max(min, 1)", s.Step)
		}
		if s.Cooldown <= 0 {
			s.Cooldown = 30 * time.Second
		}
	}
	return nil
}
