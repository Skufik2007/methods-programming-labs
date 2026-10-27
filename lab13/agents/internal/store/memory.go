package store

import (
	"context"
	"sort"
	"strconv"
	"sync"
)

// Memory — потокобезопасная реализация в памяти (триаж без Redis, тесты).
type Memory struct {
	mu     sync.Mutex
	strs   map[string]string
	zsets  map[string]map[string]float64
	hashes map[string]map[string]string
}

func NewMemory() *Memory {
	return &Memory{
		strs:   map[string]string{},
		zsets:  map[string]map[string]float64{},
		hashes: map[string]map[string]string{},
	}
}

func (m *Memory) SetNX(_ context.Context, key, value string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.strs[key]; ok {
		return false, nil
	}
	m.strs[key] = value
	return true, nil
}

func (m *Memory) ZAdd(_ context.Context, key string, score float64, member string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.zsets[key] == nil {
		m.zsets[key] = map[string]float64{}
	}
	m.zsets[key][member] = score
	return nil
}

func (m *Memory) ZRangeByScore(_ context.Context, key string, min, max float64, limit int64) ([]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	type pair struct {
		member string
		score  float64
	}
	var found []pair
	for member, score := range m.zsets[key] {
		if score >= min && score <= max {
			found = append(found, pair{member, score})
		}
	}
	sort.Slice(found, func(i, j int) bool {
		if found[i].score != found[j].score {
			return found[i].score < found[j].score
		}
		return found[i].member < found[j].member
	})
	if limit > 0 && int64(len(found)) > limit {
		found = found[:limit]
	}
	out := make([]string, len(found))
	for i, p := range found {
		out[i] = p.member
	}
	return out, nil
}

func (m *Memory) ZRem(_ context.Context, key, member string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.zsets[key][member]; !ok {
		return false, nil
	}
	delete(m.zsets[key], member)
	return true, nil
}

func (m *Memory) HSet(_ context.Context, key string, values map[string]string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.hashes[key] == nil {
		m.hashes[key] = map[string]string{}
	}
	for k, v := range values {
		m.hashes[key][k] = v
	}
	return nil
}

func (m *Memory) HGetAll(_ context.Context, key string) (map[string]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]string, len(m.hashes[key]))
	for k, v := range m.hashes[key] {
		out[k] = v
	}
	return out, nil
}

func (m *Memory) HIncrBy(_ context.Context, key, field string, n int64) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.hashes[key] == nil {
		m.hashes[key] = map[string]string{}
	}
	cur, _ := strconv.ParseInt(m.hashes[key][field], 10, 64)
	cur += n
	m.hashes[key][field] = strconv.FormatInt(cur, 10)
	return cur, nil
}

func (m *Memory) Del(_ context.Context, keys ...string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, k := range keys {
		delete(m.strs, k)
		delete(m.zsets, k)
		delete(m.hashes, k)
	}
	return nil
}

func (m *Memory) Ping(context.Context) error { return nil }
func (m *Memory) Close() error               { return nil }
