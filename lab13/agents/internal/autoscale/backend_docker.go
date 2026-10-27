package autoscale

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	labelManaged = "mas.autoscaler"
	labelStep    = "mas.step"
	dockerAPI    = "http://docker/v1.41" // хост фиктивный: соединение идёт по unix-сокету
)

// DockerBackend управляет контейнерами через Docker Engine API (без SDK).
type DockerBackend struct {
	cfg  DockerConfig
	http *http.Client
	log  *slog.Logger
	mu   sync.Mutex // сериализует Start: номер экземпляра выбирается по списку контейнеров
}

func NewDockerBackend(c DockerConfig, log *slog.Logger) *DockerBackend {
	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", c.Socket)
		},
	}
	return &DockerBackend{cfg: c, http: &http.Client{Transport: tr, Timeout: c.StopTimeout + 30*time.Second}, log: log}
}

// call выполняет запрос и при успехе (2xx) возвращает тело.
func (d *DockerBackend) call(ctx context.Context, method, path string, body any) ([]byte, error) {
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rd = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, dockerAPI+path, rd)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := d.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("docker %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return data, &dockerError{Status: resp.StatusCode, Msg: strings.TrimSpace(string(data)), Path: path}
	}
	return data, nil
}

type dockerError struct {
	Status int
	Msg    string
	Path   string
}

func (e *dockerError) Error() string {
	return fmt.Sprintf("docker %s: HTTP %d: %s", e.Path, e.Status, e.Msg)
}

type containerSummary struct {
	ID     string            `json:"Id"`
	Names  []string          `json:"Names"`
	State  string            `json:"State"`
	Labels map[string]string `json:"Labels"`
}

// listAll возвращает контейнеры автомасштабировщика (все состояния) для шага; step=="" — все шаги.
func (d *DockerBackend) listAll(ctx context.Context, step string) ([]containerSummary, error) {
	labels := []string{labelManaged + "=1"}
	if step != "" {
		labels = append(labels, labelStep+"="+step)
	}
	filters, _ := json.Marshal(map[string][]string{"label": labels})
	data, err := d.call(ctx, http.MethodGet, "/containers/json?all=1&filters="+url.QueryEscape(string(filters)), nil)
	if err != nil {
		return nil, err
	}
	var out []containerSummary
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("разбор списка контейнеров: %w", err)
	}
	return out, nil
}

// seqOf достаёт n из имени «<step>-auto-<n>».
func seqOf(step, name string) (int, bool) {
	name = strings.TrimPrefix(name, "/")
	prefix := step + "-auto-"
	if !strings.HasPrefix(name, prefix) {
		return 0, false
	}
	n, err := strconv.Atoi(strings.TrimPrefix(name, prefix))
	return n, err == nil
}

func (c containerSummary) name() string {
	if len(c.Names) == 0 {
		return ""
	}
	return strings.TrimPrefix(c.Names[0], "/")
}

// List подхватывает свои уже запущенные контейнеры — так рестарт автомасштабировщика
// не плодит лишние экземпляры. Остановившиеся контейнеры удаляются.
func (d *DockerBackend) List(ctx context.Context, step string) ([]Instance, error) {
	cs, err := d.listAll(ctx, step)
	if err != nil {
		return nil, err
	}
	var out []Instance
	for _, c := range cs {
		seq, ok := seqOf(step, c.name())
		if !ok {
			continue
		}
		if c.State == "running" || c.State == "restarting" || c.State == "created" {
			out = append(out, Instance{ID: c.name(), Step: step, Seq: seq})
			continue
		}
		// exited/dead: агент упал или остановлен снаружи — убираем, чтобы освободить имя.
		if err := d.remove(ctx, c.ID); err != nil {
			d.log.Warn("не удалось удалить завершившийся контейнер", "name", c.name(), "err", err)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, nil
}

func (d *DockerBackend) Start(ctx context.Context, sc StepConfig) (Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	cs, err := d.listAll(ctx, sc.Step)
	if err != nil {
		return Instance{}, err
	}
	seq := 0
	for _, c := range cs {
		if n, ok := seqOf(sc.Step, c.name()); ok && n > seq {
			seq = n
		}
	}
	seq++
	inst := Instance{ID: instanceID(sc.Step, seq), Step: sc.Step, Seq: seq}

	env := []string{"AGENT_ID=" + inst.ID}
	keys := make([]string, 0, len(d.cfg.Env))
	for k := range d.cfg.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		env = append(env, k+"="+d.cfg.Env[k])
	}
	hostCfg := map[string]any{"RestartPolicy": map[string]any{"Name": "no"}}
	if len(d.cfg.Binds) > 0 {
		hostCfg["Binds"] = d.cfg.Binds
	}
	body := map[string]any{
		"Image":      d.cfg.Image,
		"Cmd":        []string{d.cfg.AgentBin, "-role", sc.ContainerRole},
		"Env":        env,
		"Labels":     map[string]string{labelManaged: "1", labelStep: sc.Step},
		"HostConfig": hostCfg,
	}
	if d.cfg.Network != "" {
		hostCfg["NetworkMode"] = d.cfg.Network
		body["NetworkingConfig"] = map[string]any{
			"EndpointsConfig": map[string]any{d.cfg.Network: map[string]any{}},
		}
	}
	data, err := d.call(ctx, http.MethodPost, "/containers/create?name="+url.QueryEscape(inst.ID), body)
	if err != nil {
		return Instance{}, err
	}
	var created struct {
		ID string `json:"Id"`
	}
	if err := json.Unmarshal(data, &created); err != nil || created.ID == "" {
		return Instance{}, fmt.Errorf("разбор ответа create: %v", err)
	}
	if _, err := d.call(ctx, http.MethodPost, "/containers/"+created.ID+"/start", nil); err != nil {
		_ = d.remove(context.WithoutCancel(ctx), created.ID)
		return Instance{}, err
	}
	return inst, nil
}

// Stop останавливает контейнер с таймаутом (SIGTERM, затем SIGKILL) и удаляет его.
func (d *DockerBackend) Stop(ctx context.Context, inst Instance) error {
	t := int(d.cfg.StopTimeout / time.Second)
	_, err := d.call(ctx, http.MethodPost, "/containers/"+url.PathEscape(inst.ID)+"/stop?t="+strconv.Itoa(t), nil)
	if de, ok := err.(*dockerError); ok && (de.Status == http.StatusNotFound) {
		return nil
	}
	// 304 «уже остановлен» — не ошибка (попадает в 3xx и обрабатывается как dockerError).
	if de, ok := err.(*dockerError); ok && de.Status == http.StatusNotModified {
		err = nil
	}
	if err != nil {
		return err
	}
	return d.remove(ctx, inst.ID)
}

func (d *DockerBackend) remove(ctx context.Context, id string) error {
	_, err := d.call(ctx, http.MethodDelete, "/containers/"+url.PathEscape(id)+"?force=1", nil)
	if de, ok := err.(*dockerError); ok && de.Status == http.StatusNotFound {
		return nil
	}
	return err
}
