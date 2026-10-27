package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/bus"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/testutil"
)

// buildAgent собирает бинарник агента во временный каталог (один раз на тест).
func buildAgent(t *testing.T) string {
	t.Helper()
	goBin := filepath.Join(runtime.GOROOT(), "bin", "go")
	if _, err := os.Stat(goBin); err != nil {
		p, lookErr := exec.LookPath("go")
		if lookErr != nil {
			t.Skipf("команда go не найдена: %v", lookErr)
		}
		goBin = p
	}
	name := "agent"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	out := filepath.Join(t.TempDir(), name)
	cmd := exec.Command(goBin, "build", "-o", out, ".")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("сборка агента: %v\n%s", err, stderr.String())
	}
	return out
}

// runAgent запускает бинарник и возвращает код выхода и вывод (для сценариев, где агент
// завершается сам: ошибки конфигурации и роли).
func runAgent(t *testing.T, bin string, env []string, args ...string) (int, string) {
	t.Helper()
	cmd := exec.Command(bin, args...)
	cmd.Env = append(os.Environ(), env...)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	done := make(chan error, 1)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err == nil {
			return 0, out.String()
		}
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode(), out.String()
		}
		t.Fatalf("запуск агента: %v", err)
	case <-time.After(20 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatal("агент не завершился сам")
	}
	return -1, out.String()
}

// Ошибки запуска: нет роли, роль с неизвестным видом, неверный часовой пояс — код выхода 2.
func TestAgentConfigErrorsExitWithCode2(t *testing.T) {
	bin := buildAgent(t)

	if code, out := runAgent(t, bin, nil); code != exitConfig || !strings.Contains(out, "-role") {
		t.Fatalf("без -role: код %d, вывод %q", code, out)
	}

	bad := filepath.Join(t.TempDir(), "bad.md")
	if err := os.WriteFile(bad, []byte("# Роль: X\n\n## Параметры\n```yaml\nkind: dentist\nstep: x\n```\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, out := runAgent(t, bin, nil, "-role", bad); code != exitConfig || !strings.Contains(out, "неизвестный kind") {
		t.Fatalf("неизвестный kind: код %d, вывод %q", code, out)
	}

	triageRole := filepath.Join("..", "..", "..", "roles", "triage.md")
	if code, out := runAgent(t, bin, []string{"CLINIC_TZ=Mars/Olympus"}, "-role", triageRole); code != exitConfig ||
		!strings.Contains(out, "CLINIC_TZ") {
		t.Fatalf("неверный пояс: код %d, вывод %q", code, out)
	}

	if code, out := runAgent(t, bin, []string{"AGENT_FAIL_RATE=2"}, "-role", triageRole); code != exitConfig ||
		!strings.Contains(out, "AGENT_FAIL_RATE") {
		t.Fatalf("неверная доля сбоев: код %d, вывод %q", code, out)
	}
}

// Сквозной сценарий: собранный агент триажа поднимается с NATS, принимает задачу из
// tasks.triage и публикует результат в results.<run_id>. Хранилище — в памяти (REDIS_URL не задан).
func TestBuiltTriageAgentProcessesTask(t *testing.T) {
	bin := buildAgent(t)
	url := testutil.StartNATS(t)
	triageRole, err := filepath.Abs(filepath.Join("..", "..", "..", "roles", "triage.md"))
	if err != nil {
		t.Fatal(err)
	}

	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	results, err := nc.SubscribeSync("results.run-e2e")
	if err != nil {
		t.Fatal(err)
	}
	hb, err := nc.SubscribeSync(bus.HeartbeatSubject)
	if err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command(bin, "-role", triageRole)
	cmd.Env = append(os.Environ(),
		"NATS_URL="+url,
		"AGENT_ID=triage-e2e",
		"LOG_LEVEL=warn",
		"OTEL_SDK_DISABLED=true",
	)
	var logs bytes.Buffer
	cmd.Stdout, cmd.Stderr = &logs, &logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})

	// Агент сообщает о готовности heartbeat-ом.
	hbMsg, err := hb.NextMsg(10 * time.Second)
	if err != nil {
		t.Fatalf("heartbeat не пришёл: %v\n%s", err, logs.String())
	}
	var hbPayload struct {
		AgentID string `json:"agent_id"`
		Role    string `json:"role"`
		Lang    string `json:"lang"`
	}
	if err := json.Unmarshal(hbMsg.Data, &hbPayload); err != nil {
		t.Fatal(err)
	}
	if hbPayload.AgentID != "triage-e2e" || hbPayload.Role != "triage" || hbPayload.Lang != "go" {
		t.Fatalf("heartbeat: %+v", hbPayload)
	}

	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(map[string]any{
		"case":     map[string]any{"complaint": "Сильная боль в груди при ходьбе", "age": 50},
		"previous": map[string]any{"red_flags": []string{}},
	})
	task, _ := json.Marshal(core.Task{TaskID: "task-e2e", RunID: "run-e2e", Step: "triage", Kind: "triage",
		Attempt: 1, Payload: payload})

	// Поток TASKS создаёт сам агент сразу после heartbeat; ждём, пока публикация пройдёт.
	deadline := time.Now().Add(10 * time.Second)
	for {
		_, err := js.PublishMsg(context.Background(), &nats.Msg{Subject: "tasks.triage", Data: task,
			Header: nats.Header{"Nats-Msg-Id": []string{"task-e2e-1"}}})
		if err == nil || time.Now().After(deadline) {
			if err != nil {
				t.Fatalf("публикация задачи: %v\n%s", err, logs.String())
			}
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	res, err := results.NextMsg(10 * time.Second)
	if err != nil {
		t.Fatalf("результат не пришёл: %v\n%s", err, logs.String())
	}
	var r core.Result
	if err := json.Unmarshal(res.Data, &r); err != nil {
		t.Fatal(err)
	}
	if !r.Success || r.AgentID != "triage-e2e" {
		t.Fatalf("результат: %+v", r)
	}
	out, _ := r.Output.(map[string]any)
	if out["urgency"] != "emergency" || out["recommendation"] != "Вызовите скорую: 103" {
		t.Fatalf("боль в груди — emergency: %v", out)
	}
}
