package autoscale

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeFile(t *testing.T, name, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func noEnv(string) string { return "" }

func TestLoadConfigDefaults(t *testing.T) {
	t.Parallel()
	p := writeFile(t, "a.yaml", "steps:\n  - step: triage\n    role: roles/triage.md\n    per_instance: 5\n    max: 3\n")
	c, err := LoadConfig(p, noEnv)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if c.Backend != "exec" || c.Interval != 2*time.Second || c.StatusPort != 8090 {
		t.Errorf("значения по умолчанию: backend=%s interval=%v port=%d", c.Backend, c.Interval, c.StatusPort)
	}
	if c.NATSURL != "nats://localhost:4222" || c.Exec.AgentBin != "agent" || c.Exec.StopTimeout != 15*time.Second {
		t.Errorf("exec/NATS по умолчанию: %+v", c)
	}
	if c.Docker.Socket != "/var/run/docker.sock" || c.Docker.AgentBin != "/app/agent" {
		t.Errorf("docker по умолчанию: %+v", c.Docker)
	}
	s := c.Steps[0]
	if s.Cooldown != 30*time.Second || s.ContainerRole != "roles/triage.md" || s.Min != 0 {
		t.Errorf("шаг по умолчанию: %+v", s)
	}
}

func TestLoadConfigNATSFromEnv(t *testing.T) {
	t.Parallel()
	p := writeFile(t, "a.yaml", "steps:\n  - step: triage\n    role: r\n    per_instance: 1\n    max: 1\n")
	c, err := LoadConfig(p, func(k string) string {
		if k == "NATS_URL" {
			return "nats://from-env:4222"
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.NATSURL != "nats://from-env:4222" {
		t.Errorf("NATS_URL из окружения: %q", c.NATSURL)
	}
}

func TestLoadConfigStepFromRoleFile(t *testing.T) {
	t.Parallel()
	role := writeFile(t, "triage.md", "# Роль: Триаж\n\n## Параметры\n```yaml\nkind: triage\nstep: triage\n```\n")
	p := writeFile(t, "a.yaml", "steps:\n  - role: "+filepath.ToSlash(role)+"\n    per_instance: 2\n    max: 2\n")
	c, err := LoadConfig(p, noEnv)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if c.Steps[0].Step != "triage" {
		t.Fatalf("имя шага должно браться из роли: %q", c.Steps[0].Step)
	}
}

func TestLoadConfigErrors(t *testing.T) {
	t.Parallel()
	missingRole := filepath.Join(t.TempDir(), "нет.md")
	cases := []struct {
		name    string
		yaml    string
		wantErr string
	}{
		{"неизвестный backend", "backend: kubernetes\nsteps:\n  - step: t\n    role: r\n    per_instance: 1\n    max: 1\n", "backend"},
		{"docker без образа", "backend: docker\nsteps:\n  - step: t\n    role: r\n    per_instance: 1\n    max: 1\n", "docker.image"},
		{"нет шагов", "backend: exec\n", "ни один шаг"},
		{"шаг без step и role", "steps:\n  - per_instance: 1\n    max: 1\n", "нужен step или role"},
		{"роль из имени не читается", "steps:\n  - role: " + filepath.ToSlash(missingRole) + "\n    per_instance: 1\n    max: 1\n", "роль не прочитать"},
		{"роль с неверным видом", "steps:\n  - role: " + roleFile(t, "# Роль: X\n\n## Параметры\n```yaml\nkind: dentist\nstep: x\n```\n") + "\n    per_instance: 1\n    max: 1\n", "неизвестный kind"},
		{"шаг указан дважды", "steps:\n  - step: t\n    role: r\n    per_instance: 1\n    max: 1\n  - step: t\n    role: r\n    per_instance: 1\n    max: 1\n", "указан дважды"},
		{"нет role", "steps:\n  - step: t\n    per_instance: 1\n    max: 1\n", "не задан role"},
		{"per_instance 0", "steps:\n  - step: t\n    role: r\n    per_instance: 0\n    max: 1\n", "per_instance"},
		{"min отрицательный", "steps:\n  - step: t\n    role: r\n    per_instance: 1\n    min: -1\n    max: 1\n", "min < 0"},
		{"max 0", "steps:\n  - step: t\n    role: r\n    per_instance: 1\n    max: 0\n", "max должен"},
		{"max меньше min", "steps:\n  - step: t\n    role: r\n    per_instance: 1\n    min: 3\n    max: 2\n", "max должен"},
		{"сырой YAML", "steps: [\n", "разбор"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			p := writeFile(t, "a.yaml", c.yaml)
			_, err := LoadConfig(p, noEnv)
			if err == nil || !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("ошибка %v, ожидалось %q", err, c.wantErr)
			}
		})
	}
	if _, err := LoadConfig(filepath.Join(t.TempDir(), "нет.yaml"), noEnv); err == nil {
		t.Error("отсутствующий файл конфигурации — ошибка чтения")
	}
}

func roleFile(t *testing.T, content string) string {
	t.Helper()
	return filepath.ToSlash(writeFile(t, "role.md", content))
}

func TestLoadConfigDockerAndDuplicateStepOK(t *testing.T) {
	t.Parallel()
	p := writeFile(t, "d.yaml", `backend: docker
nats_url: nats://nats:4222
docker:
  image: mas-agent:latest
  network: mas_default
  env:
    NATS_URL: nats://nats:4222
steps:
  - step: triage
    role: roles/triage.md
    container_role: /app/roles/triage.md
    per_instance: 5
    min: 1
    max: 3
    cooldown: 45s
`)
	c, err := LoadConfig(p, noEnv)
	if err != nil {
		t.Fatalf("docker-конфигурация: %v", err)
	}
	if c.Backend != "docker" || c.Docker.Image != "mas-agent:latest" || c.NATSURL != "nats://nats:4222" {
		t.Errorf("docker: %+v", c)
	}
	if c.Steps[0].Cooldown != 45*time.Second || c.Steps[0].ContainerRole != "/app/roles/triage.md" {
		t.Errorf("шаг: %+v", c.Steps[0])
	}
	if c.Docker.Env["NATS_URL"] != "nats://nats:4222" {
		t.Errorf("env: %v", c.Docker.Env)
	}
}

// Файлы из каталога deploy/ должны разбираться без ошибок (контракт раздела 12).
func TestDeployConfigsLoad(t *testing.T) {
	t.Parallel()
	for _, name := range []string{"autoscaler.local.yaml", "autoscaler.docker.yaml"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			c, err := LoadConfig(filepath.Join("..", "..", "..", "deploy", name), noEnv)
			if err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			if len(c.Steps) == 0 || c.Steps[0].Step != "triage" {
				t.Fatalf("%s: шаги %+v", name, c.Steps)
			}
			if c.Steps[0].Min > c.Steps[0].Max {
				t.Errorf("%s: min > max", name)
			}
		})
	}
}
