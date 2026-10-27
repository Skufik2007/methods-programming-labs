package collector

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

// Assigner отвечает на вопрос «какие устройства мои». Devices вызывается каждый
// цикл опроса и должен быть дешёвым (etcd-реализация вернёт кэш из watch).
// Close вызывается после записи чекпоинтов (отзыв lease в etcd).
type Assigner interface {
	Devices(ctx context.Context) ([]string, error)
	Close(ctx context.Context) error
}

// StaticAssigner — фиксированный список; если он пуст, берёт все устройства
// эмулятора через lister (и кэширует успешный результат).
type StaticAssigner struct {
	mu     sync.Mutex
	list   []string
	lister func(ctx context.Context) ([]string, error)
}

func NewStaticAssigner(list []string, lister func(ctx context.Context) ([]string, error)) *StaticAssigner {
	return &StaticAssigner{list: list, lister: lister}
}

func (a *StaticAssigner) Devices(ctx context.Context) ([]string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.list) == 0 && a.lister != nil {
		l, err := a.lister(ctx)
		if err != nil {
			return nil, err
		}
		a.list = l
	}
	return a.list, nil
}

func (a *StaticAssigner) Close(context.Context) error { return nil }

// CheckpointStore хранит последний записанный seq по устройствам. Save обязан быть
// монотонным: значение, меньшее сохранённого, не откатывает чекпоинт.
type CheckpointStore interface {
	Load(ctx context.Context, device string) (int64, error)
	Save(ctx context.Context, seqs map[string]int64) error
}

// MemoryStore — чекпоинты в памяти.
type MemoryStore struct {
	mu sync.Mutex
	m  map[string]int64
}

func NewMemoryStore() *MemoryStore { return &MemoryStore{m: map[string]int64{}} }

func (s *MemoryStore) Load(_ context.Context, device string) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.m[device], nil
}

func (s *MemoryStore) Save(_ context.Context, seqs map[string]int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.merge(seqs)
	return nil
}

func (s *MemoryStore) merge(seqs map[string]int64) {
	for d, v := range seqs {
		if v > s.m[d] {
			s.m[d] = v
		}
	}
}

// Snapshot — копия состояния (для тестов и /status).
func (s *MemoryStore) Snapshot() map[string]int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := make(map[string]int64, len(s.m))
	for k, v := range s.m {
		c[k] = v
	}
	return c
}

// FileStore — память + файл JSON (OUT_DIR/checkpoints.json), запись атомарная.
type FileStore struct {
	path string
	mem  *MemoryStore
	mu   sync.Mutex // сериализует запись файла
}

func NewFileStore(path string) (*FileStore, error) {
	fs := &FileStore{path: path, mem: NewMemoryStore()}
	b, err := os.ReadFile(path)
	switch {
	case os.IsNotExist(err):
	case err != nil:
		return nil, err
	default:
		var m map[string]int64
		if err := json.Unmarshal(b, &m); err != nil {
			return nil, fmt.Errorf("файл чекпоинтов %s повреждён: %w", path, err)
		}
		fs.mem.merge(m)
	}
	return fs, nil
}

func (s *FileStore) Load(ctx context.Context, device string) (int64, error) {
	return s.mem.Load(ctx, device)
}

func (s *FileStore) Save(ctx context.Context, seqs map[string]int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.mem.Save(ctx, seqs)
	b, err := json.Marshal(s.mem.Snapshot())
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// DeviceInfo — запись из GET /v1/devices.
type DeviceInfo struct {
	DeviceID  string   `json:"device_id"`
	PatientID string   `json:"patient_id"`
	Metrics   []string `json:"metrics"`
	PeriodMs  int64    `json:"period_ms"`
}

type ReadingsResponse struct {
	DeviceID string          `json:"device_id"`
	Readings []model.Reading `json:"readings"`
	LastSeq  int64           `json:"last_seq"`
}

// Source — источник показаний (HTTP-клиент эмулятора; в тестах — заглушка).
type Source interface {
	Devices(ctx context.Context) ([]string, error)
	Readings(ctx context.Context, device string, afterSeq int64, limit int) (ReadingsResponse, error)
}

type HTTPSource struct {
	base   string
	client *http.Client
}

func NewHTTPSource(base string, timeout time.Duration, conns int) *HTTPSource {
	tr := &http.Transport{MaxIdleConns: conns * 2, MaxIdleConnsPerHost: conns * 2, IdleConnTimeout: 60 * time.Second}
	return &HTTPSource{base: base, client: &http.Client{Transport: tr, Timeout: timeout}}
}

func (h *HTTPSource) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.base+path, nil)
	if err != nil {
		return err
	}
	resp, err := h.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var buf bytes.Buffer
		_, _ = buf.ReadFrom(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("GET %s: %s %s", path, resp.Status, buf.String())
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (h *HTTPSource) Devices(ctx context.Context) ([]string, error) {
	var r struct {
		Devices []DeviceInfo `json:"devices"`
	}
	if err := h.get(ctx, "/v1/devices", &r); err != nil {
		return nil, err
	}
	ids := make([]string, len(r.Devices))
	for i, d := range r.Devices {
		ids[i] = d.DeviceID
	}
	return ids, nil
}

func (h *HTTPSource) Readings(ctx context.Context, device string, after int64, limit int) (ReadingsResponse, error) {
	var r ReadingsResponse
	err := h.get(ctx, "/v1/devices/"+url.PathEscape(device)+"/readings?after_seq="+strconv.FormatInt(after, 10)+"&limit="+strconv.Itoa(limit), &r)
	return r, err
}
