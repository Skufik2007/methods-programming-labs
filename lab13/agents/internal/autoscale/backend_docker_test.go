package autoscale

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// dockerMock имитирует Docker Engine API v1.41 (нужные эндпоинты) и запоминает запросы.
type dockerMock struct {
	mu         sync.Mutex
	containers []containerSummary // ответ GET /containers/json
	createBody map[string]any     // тело последнего create
	createName string             // имя из query
	createCode int                // код ответа create (0 — 201)
	startCode  int                // код ответа start (0 — 204)
	stopCode   int                // код ответа stop (0 — 204)
	removeCode int                // код ответа delete (0 — 204)
	listCode   int                // код ответа list (0 — 200)
	listBody   string             // сырой ответ list, если задан
	deleted    []string
	stopped    []string
	listFilter string
}

func (m *dockerMock) handler(t *testing.T) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1.41/containers/json", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.listFilter = r.URL.Query().Get("filters")
		if m.listCode != 0 {
			http.Error(w, "list failed", m.listCode)
			return
		}
		if m.listBody != "" {
			_, _ = io.WriteString(w, m.listBody)
			return
		}
		_ = json.NewEncoder(w).Encode(m.containers)
	})
	mux.HandleFunc("POST /v1.41/containers/create", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.createName = r.URL.Query().Get("name")
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("тело create: %v", err)
		}
		m.createBody = body
		if m.createCode != 0 {
			http.Error(w, "conflict", m.createCode)
			return
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"Id":"new-container-id"}`)
	})
	mux.HandleFunc("POST /v1.41/containers/{id}/start", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		if m.startCode != 0 {
			http.Error(w, "cannot start", m.startCode)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("POST /v1.41/containers/{id}/stop", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.stopped = append(m.stopped, r.PathValue("id")+"?t="+r.URL.Query().Get("t"))
		if m.stopCode != 0 {
			if m.stopCode == http.StatusNotModified {
				w.WriteHeader(http.StatusNotModified)
				return
			}
			http.Error(w, "stop failed", m.stopCode)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("DELETE /v1.41/containers/{id}", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.deleted = append(m.deleted, r.PathValue("id")+"?force="+r.URL.Query().Get("force"))
		if m.removeCode != 0 {
			http.Error(w, "remove failed", m.removeCode)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	return mux
}

// newDockerTest поднимает httptest-сервер и подключает к нему DockerBackend.
// Соединение идёт по TCP к серверу вместо unix-сокета Docker.
func newDockerTest(t *testing.T, cfg DockerConfig, m *dockerMock) *DockerBackend {
	t.Helper()
	srv := httptest.NewServer(m.handler(t))
	t.Cleanup(srv.Close)
	addr := srv.Listener.Addr().String()
	d := NewDockerBackend(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	d.http = &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dl net.Dialer
			return dl.DialContext(ctx, "tcp", addr)
		}},
	}
	return d
}

func baseDockerCfg() DockerConfig {
	return DockerConfig{
		Socket: "/var/run/docker.sock", Image: "mas-agent:latest", Network: "mas_default",
		AgentBin: "/app/agent", StopTimeout: 15 * time.Second,
		Env:   map[string]string{"NATS_URL": "nats://nats:4222", "REDIS_URL": "redis://redis:6379/0"},
		Binds: []string{"/host/roles:/app/roles:ro"},
	}
}

func TestDockerListFiltersAndCleansUp(t *testing.T) {
	t.Parallel()
	m := &dockerMock{containers: []containerSummary{
		{ID: "c3", Names: []string{"/triage-auto-3"}, State: "created"},
		{ID: "c1", Names: []string{"/triage-auto-1"}, State: "running"},
		{ID: "c2", Names: []string{"/triage-auto-2"}, State: "exited"},   // упал — удаляем
		{ID: "cx", Names: []string{"/booking-auto-1"}, State: "running"}, // другой шаг
		{ID: "cy", Names: []string{"/triage-manual"}, State: "running"},  // не наш формат
	}}
	d := newDockerTest(t, baseDockerCfg(), m)
	got, err := d.List(context.Background(), "triage")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].ID != "triage-auto-1" || got[1].ID != "triage-auto-3" || got[1].Seq != 3 {
		t.Fatalf("список: %+v", got)
	}
	// фильтр по меткам управляемых контейнеров шага
	filter := m.listFilter
	for _, want := range []string{"mas.autoscaler=1", "mas.step=triage"} {
		if !strings.Contains(filter, want) {
			t.Errorf("фильтр %q не содержит %q", filter, want)
		}
	}
	if len(m.deleted) != 1 || m.deleted[0] != "c2?force=1" {
		t.Errorf("завершившийся контейнер должен быть удалён: %v", m.deleted)
	}
}

func TestDockerListErrors(t *testing.T) {
	t.Parallel()
	t.Run("HTTP-ошибка", func(t *testing.T) {
		t.Parallel()
		d := newDockerTest(t, baseDockerCfg(), &dockerMock{listCode: http.StatusInternalServerError})
		if _, err := d.List(context.Background(), "triage"); err == nil || !strings.Contains(err.Error(), "500") {
			t.Fatalf("ожидалась ошибка 500: %v", err)
		}
	})
	t.Run("неверный JSON", func(t *testing.T) {
		t.Parallel()
		d := newDockerTest(t, baseDockerCfg(), &dockerMock{listBody: "не json"})
		if _, err := d.List(context.Background(), "triage"); err == nil || !strings.Contains(err.Error(), "разбор") {
			t.Fatalf("ожидалась ошибка разбора: %v", err)
		}
	})
	t.Run("сервер недоступен", func(t *testing.T) {
		t.Parallel()
		m := &dockerMock{}
		srv := httptest.NewServer(m.handler(t))
		d := newDockerTest(t, baseDockerCfg(), m)
		srv.Close()
		// подключение по адресу закрытого сервера
		d.http = &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dl net.Dialer
			return dl.DialContext(ctx, "tcp", srv.Listener.Addr().String())
		}}}
		if _, err := d.List(context.Background(), "triage"); err == nil {
			t.Fatal("ожидалась ошибка соединения")
		}
	})
}

func TestDockerStartCreatesContainerWithContract(t *testing.T) {
	t.Parallel()
	m := &dockerMock{containers: []containerSummary{
		{ID: "c1", Names: []string{"/triage-auto-1"}, State: "running"},
		{ID: "c2", Names: []string{"/triage-auto-2"}, State: "running"},
	}}
	d := newDockerTest(t, baseDockerCfg(), m)
	sc := StepConfig{Step: "triage", Role: "roles/triage.md", ContainerRole: "/app/roles/triage.md"}
	inst, err := d.Start(context.Background(), sc)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if inst.ID != "triage-auto-3" || inst.Seq != 3 || inst.Step != "triage" {
		t.Fatalf("экземпляр: %+v", inst)
	}
	if m.createName != "triage-auto-3" {
		t.Errorf("имя контейнера %q", m.createName)
	}
	body := m.createBody
	cmd, _ := body["Cmd"].([]any)
	if len(cmd) != 3 || cmd[0] != "/app/agent" || cmd[1] != "-role" || cmd[2] != "/app/roles/triage.md" {
		t.Errorf("команда: %v", cmd)
	}
	env, _ := body["Env"].([]any)
	joined := joinAny(env)
	for _, want := range []string{"AGENT_ID=triage-auto-3", "NATS_URL=nats://nats:4222", "REDIS_URL=redis://redis:6379/0"} {
		if !strings.Contains(joined, want) {
			t.Errorf("переменная %q не передана: %s", want, joined)
		}
	}
	labels, _ := body["Labels"].(map[string]any)
	if labels["mas.autoscaler"] != "1" || labels["mas.step"] != "triage" {
		t.Errorf("метки: %v", labels)
	}
	host, _ := body["HostConfig"].(map[string]any)
	if policy, _ := host["RestartPolicy"].(map[string]any); policy["Name"] != "no" {
		t.Errorf("перезапуск должен быть выключен: %v", host["RestartPolicy"])
	}
	if host["NetworkMode"] != "mas_default" {
		t.Errorf("сеть compose: %v", host["NetworkMode"])
	}
	if binds, _ := host["Binds"].([]any); len(binds) != 1 {
		t.Errorf("тома: %v", host["Binds"])
	}
	netCfg, _ := body["NetworkingConfig"].(map[string]any)
	endpoints, _ := netCfg["EndpointsConfig"].(map[string]any)
	if _, ok := endpoints["mas_default"]; !ok {
		t.Errorf("NetworkingConfig: %v", netCfg)
	}
}

func joinAny(list []any) string {
	parts := make([]string, 0, len(list))
	for _, v := range list {
		s, _ := v.(string)
		parts = append(parts, s)
	}
	return strings.Join(parts, ";")
}

func TestDockerStartWithoutNetworkAndBinds(t *testing.T) {
	t.Parallel()
	cfg := baseDockerCfg()
	cfg.Network = ""
	cfg.Binds = nil
	m := &dockerMock{}
	d := newDockerTest(t, cfg, m)
	if _, err := d.Start(context.Background(), StepConfig{Step: "reminder", ContainerRole: "/app/roles/reminder.md"}); err != nil {
		t.Fatal(err)
	}
	host, _ := m.createBody["HostConfig"].(map[string]any)
	if _, ok := host["NetworkMode"]; ok {
		t.Error("без сети NetworkMode не задаётся")
	}
	if _, ok := host["Binds"]; ok {
		t.Error("без томов Binds не задаётся")
	}
	if _, ok := m.createBody["NetworkingConfig"]; ok {
		t.Error("без сети NetworkingConfig не задаётся")
	}
}

func TestDockerStartErrors(t *testing.T) {
	t.Parallel()
	sc := StepConfig{Step: "triage", ContainerRole: "roles/triage.md"}
	t.Run("create отказал — ошибка", func(t *testing.T) {
		t.Parallel()
		d := newDockerTest(t, baseDockerCfg(), &dockerMock{createCode: http.StatusConflict})
		if _, err := d.Start(context.Background(), sc); err == nil || !strings.Contains(err.Error(), "409") {
			t.Fatalf("ожидалась ошибка 409: %v", err)
		}
	})
	t.Run("start отказал — контейнер удаляется", func(t *testing.T) {
		t.Parallel()
		m := &dockerMock{startCode: http.StatusInternalServerError}
		d := newDockerTest(t, baseDockerCfg(), m)
		if _, err := d.Start(context.Background(), sc); err == nil {
			t.Fatal("ожидалась ошибка start")
		}
		if len(m.deleted) != 1 || m.deleted[0] != "new-container-id?force=1" {
			t.Fatalf("неуспешный контейнер должен быть удалён: %v", m.deleted)
		}
	})
}

func TestDockerStartBadCreateResponse(t *testing.T) {
	t.Parallel()
	m := &dockerMock{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			_, _ = io.WriteString(w, "[]")
			return
		}
		_, _ = io.WriteString(w, `{"Warnings":[]}`) // без Id
	}))
	defer srv.Close()
	_ = m
	d := NewDockerBackend(baseDockerCfg(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	d.http = &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		var dl net.Dialer
		return dl.DialContext(ctx, "tcp", srv.Listener.Addr().String())
	}}}
	if _, err := d.Start(context.Background(), StepConfig{Step: "triage", ContainerRole: "r"}); err == nil ||
		!strings.Contains(err.Error(), "разбор ответа create") {
		t.Fatalf("ожидалась ошибка разбора ответа create: %v", err)
	}
}

func TestDockerStopOutcomes(t *testing.T) {
	t.Parallel()
	inst := Instance{ID: "triage-auto-1", Step: "triage", Seq: 1}
	t.Run("остановлен — удаляется", func(t *testing.T) {
		t.Parallel()
		m := &dockerMock{}
		d := newDockerTest(t, baseDockerCfg(), m)
		if err := d.Stop(context.Background(), inst); err != nil {
			t.Fatal(err)
		}
		if len(m.stopped) != 1 || m.stopped[0] != "triage-auto-1?t=15" {
			t.Errorf("stop: %v", m.stopped)
		}
		if len(m.deleted) != 1 {
			t.Errorf("контейнер должен быть удалён: %v", m.deleted)
		}
	})
	t.Run("304 «уже остановлен» — не ошибка, удаляем", func(t *testing.T) {
		t.Parallel()
		m := &dockerMock{stopCode: http.StatusNotModified}
		d := newDockerTest(t, baseDockerCfg(), m)
		if err := d.Stop(context.Background(), inst); err != nil {
			t.Fatalf("304 не ошибка: %v", err)
		}
		if len(m.deleted) != 1 {
			t.Error("после 304 контейнер удаляется")
		}
	})
	t.Run("404 — уже нет, не ошибка", func(t *testing.T) {
		t.Parallel()
		m := &dockerMock{stopCode: http.StatusNotFound}
		d := newDockerTest(t, baseDockerCfg(), m)
		if err := d.Stop(context.Background(), inst); err != nil {
			t.Fatalf("404 не ошибка: %v", err)
		}
		if len(m.deleted) != 0 {
			t.Error("при 404 удалять нечего")
		}
	})
	t.Run("500 — ошибка", func(t *testing.T) {
		t.Parallel()
		d := newDockerTest(t, baseDockerCfg(), &dockerMock{stopCode: http.StatusInternalServerError})
		if err := d.Stop(context.Background(), inst); err == nil {
			t.Fatal("ожидалась ошибка 500")
		}
	})
	t.Run("ошибка удаления после остановки", func(t *testing.T) {
		t.Parallel()
		d := newDockerTest(t, baseDockerCfg(), &dockerMock{removeCode: http.StatusInternalServerError})
		if err := d.Stop(context.Background(), inst); err == nil {
			t.Fatal("ошибка remove должна вернуться")
		}
	})
	t.Run("удаление 404 — не ошибка", func(t *testing.T) {
		t.Parallel()
		d := newDockerTest(t, baseDockerCfg(), &dockerMock{removeCode: http.StatusNotFound})
		if err := d.Stop(context.Background(), inst); err != nil {
			t.Fatalf("404 при удалении: %v", err)
		}
	})
}

func TestDockerRemoveDirect(t *testing.T) {
	t.Parallel()
	m := &dockerMock{removeCode: http.StatusNotFound}
	d := newDockerTest(t, baseDockerCfg(), m)
	if err := d.remove(context.Background(), "abc"); err != nil {
		t.Fatalf("remove 404: %v", err)
	}
	m2 := &dockerMock{removeCode: http.StatusForbidden}
	d2 := newDockerTest(t, baseDockerCfg(), m2)
	if err := d2.remove(context.Background(), "abc"); err == nil || !strings.Contains(err.Error(), "403") {
		t.Fatalf("remove 403: %v", err)
	}
}

func TestDockerErrorAndHelpers(t *testing.T) {
	t.Parallel()
	e := &dockerError{Status: 409, Msg: "конфликт", Path: "/containers/create"}
	if !strings.Contains(e.Error(), "HTTP 409") || !strings.Contains(e.Error(), "конфликт") {
		t.Errorf("текст ошибки: %q", e.Error())
	}
	if n, ok := seqOf("triage", "/triage-auto-12"); !ok || n != 12 {
		t.Errorf("seqOf с ведущим слэшем: %d %v", n, ok)
	}
	if _, ok := seqOf("triage", "triage-auto-x"); ok {
		t.Error("нечисловой номер не распознаётся")
	}
	if _, ok := seqOf("triage", "booking-auto-1"); ok {
		t.Error("чужой шаг не распознаётся")
	}
	if (containerSummary{}).name() != "" {
		t.Error("без имён — пустая строка")
	}
	if got := instanceID("triage", 7); got != "triage-auto-7" {
		t.Errorf("instanceID = %q", got)
	}
	// Экранирование query-параметра фильтров.
	f, _ := json.Marshal(map[string][]string{"label": {"a=1"}})
	if !strings.Contains(url.QueryEscape(string(f)), "%") {
		t.Error("фильтр должен экранироваться")
	}
}

func TestDockerStartDoesNotDoubleCountWithMixedStates(t *testing.T) {
	t.Parallel()
	// Контейнер с большим номером в состоянии exited ещё учитывается при выборе номера,
	// но List его уже удалил бы: Start берёт max по всем текущим именам.
	m := &dockerMock{containers: []containerSummary{
		{ID: "c5", Names: []string{"/triage-auto-5"}, State: "exited"},
	}}
	d := newDockerTest(t, baseDockerCfg(), m)
	inst, err := d.Start(context.Background(), StepConfig{Step: "triage", ContainerRole: "r"})
	if err != nil {
		t.Fatal(err)
	}
	if inst.Seq != 6 {
		t.Fatalf("номер %d, ожидался 6 (после максимального 5)", inst.Seq)
	}
}
