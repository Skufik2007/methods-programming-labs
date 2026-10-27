package coord

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"sync"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// saveChunk — сколько устройств обрабатывается одной транзакцией
// (лимит etcd по умолчанию — 128 операций в транзакции).
const saveChunk = 64

// CheckpointStore хранит чекпоинты в /lab14/checkpoints/<device_id>. Запись идёт
// транзакцией «только если новое значение больше»: старый владелец устройства не откатит
// чекпоинт, ушедший вперёд у нового.
type CheckpointStore struct {
	cli    *clientv3.Client
	prefix string

	mu    sync.Mutex
	floor map[string]int64 // нижняя оценка значения в etcd: позволяет не писать лишнего
}

func newCheckpointStore(cli *clientv3.Client, prefix string) *CheckpointStore {
	return &CheckpointStore{cli: cli, prefix: prefix + "checkpoints/", floor: map[string]int64{}}
}

func (s *CheckpointStore) key(device string) string { return s.prefix + device }

// Load возвращает чекпоинт устройства (0, если его нет).
func (s *CheckpointStore) Load(ctx context.Context, device string) (int64, error) {
	resp, err := s.cli.Get(ctx, s.key(device))
	if err != nil {
		return 0, err
	}
	if len(resp.Kvs) == 0 {
		return 0, nil
	}
	return parseSeq(resp.Kvs[0].Value)
}

func parseSeq(b []byte) (int64, error) {
	v, err := strconv.ParseInt(string(b), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("чекпоинт %q не число: %w", b, err)
	}
	return v, nil
}

// Greater — условие записи: новое значение строго больше текущего.
func Greater(cur, next int64) bool { return next > cur }

// Save записывает монотонно: значения, не превышающие хранимые, пропускаются.
func (s *CheckpointStore) Save(ctx context.Context, seqs map[string]int64) error {
	s.mu.Lock()
	var devs []string
	for d, v := range seqs {
		if Greater(s.floor[d], v) {
			devs = append(devs, d)
		}
	}
	s.mu.Unlock()
	sort.Strings(devs)
	for i := 0; i < len(devs); i += saveChunk {
		end := min(i+saveChunk, len(devs))
		if err := s.saveChunk(ctx, devs[i:end], seqs); err != nil {
			return err
		}
	}
	return nil
}

func (s *CheckpointStore) saveChunk(ctx context.Context, devs []string, seqs map[string]int64) error {
	var lastErr error
	for attempt := 0; attempt < 5; attempt++ {
		// 1) читаем текущие значения одной транзакцией
		gets := make([]clientv3.Op, len(devs))
		for i, d := range devs {
			gets[i] = clientv3.OpGet(s.key(d))
		}
		rr, err := s.cli.Txn(ctx).Then(gets...).Commit()
		if err != nil {
			return err
		}
		var cmps []clientv3.Cmp
		var puts []clientv3.Op
		known := map[string]int64{}
		for i, d := range devs {
			kvs := rr.Responses[i].GetResponseRange().Kvs
			cur := int64(0)
			if len(kvs) == 0 {
				cmps = append(cmps, clientv3.Compare(clientv3.CreateRevision(s.key(d)), "=", 0))
			} else {
				if cur, err = parseSeq(kvs[0].Value); err != nil {
					return err
				}
				cmps = append(cmps, clientv3.Compare(clientv3.ModRevision(s.key(d)), "=", kvs[0].ModRevision))
			}
			known[d] = cur
			if Greater(cur, seqs[d]) {
				puts = append(puts, clientv3.OpPut(s.key(d), strconv.FormatInt(seqs[d], 10)))
				known[d] = seqs[d]
			}
		}
		if len(puts) > 0 {
			// 2) пишем, только если никто не изменил ключи между чтением и записью
			resp, err := s.cli.Txn(ctx).If(cmps...).Then(puts...).Commit()
			if err != nil {
				return err
			}
			if !resp.Succeeded {
				lastErr = fmt.Errorf("конфликт записи чекпоинтов, попытка %d", attempt+1)
				continue
			}
		}
		s.mu.Lock()
		for d, v := range known {
			s.floor[d] = max(s.floor[d], v)
		}
		s.mu.Unlock()
		return nil
	}
	return lastErr
}
