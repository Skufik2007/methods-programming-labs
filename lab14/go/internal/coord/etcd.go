package coord

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

// Member — значение ключа /lab14/collectors/<collector_id>.
type Member struct {
	CollectorID string `json:"collector_id"`
	FlightAddr  string `json:"flight_addr"`
	StatusAddr  string `json:"status_addr"`
	StartedAt   string `json:"started_at"`
}

// Options — параметры подключения к etcd.
type Options struct {
	Endpoints []string
	TTL       time.Duration // ETCD_TTL: срок lease членства
	Prefix    string        // по умолчанию "/lab14/"
	Self      Member
	// Handover — сколько ждать перед подхватом устройства у живого прежнего владельца:
	// за это время тот прекратит опрос, сбросит буфер в стоки и запишет чекпоинт
	// (ядро сбрасывает пакет раз в FLUSH_INTERVAL). Для умершего владельца ожидания нет.
	Handover time.Duration
	// Devices — полный список устройств (HTTP-эмулятор или DEVICE_LIST).
	Devices func(ctx context.Context) ([]string, error)
	Log     *slog.Logger
	Now     func() time.Time // для тестов; по умолчанию time.Now
}

// Assigner — реализация collector.Assigner на HRW + watch за членами в etcd.
type Assigner struct {
	opt    Options
	cli    *clientv3.Client
	log    *slog.Logger
	prefix string
	memKey string

	cps *CheckpointStore

	cancel context.CancelFunc
	done   chan struct{}

	leaseMu sync.Mutex
	lease   clientv3.LeaseID
	stopKA  context.CancelFunc

	mu        sync.Mutex
	members   []string             // отсортированные id живых членов
	prev      []string             // члены до последнего изменения (для определения прежнего владельца)
	all       []string             // все устройства
	owned     map[string]time.Time // устройство → когда его можно начинать опрашивать
	out       []string
	dirty     bool
	nextReady time.Time

	reregs atomic.Int64
	events atomic.Int64
}

// Dial подключается к etcd, регистрирует члена и запускает наблюдение. Возвращается
// после первой успешной регистрации.
func Dial(ctx context.Context, opt Options) (*Assigner, error) {
	if opt.Prefix == "" {
		opt.Prefix = "/lab14/"
	}
	if opt.Now == nil {
		opt.Now = time.Now
	}
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	if opt.TTL < time.Second {
		opt.TTL = 10 * time.Second
	}
	cli, err := clientv3.New(clientv3.Config{Endpoints: opt.Endpoints, DialTimeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	a := &Assigner{
		opt: opt, cli: cli, log: opt.Log, prefix: opt.Prefix,
		memKey: opt.Prefix + "collectors/" + opt.Self.CollectorID,
		cps:    newCheckpointStore(cli, opt.Prefix),
		owned:  map[string]time.Time{}, dirty: true, done: make(chan struct{}),
	}
	rctx, cancel := context.WithCancel(context.Background())
	a.cancel = cancel
	ka, err := a.register(ctx)
	if err != nil {
		cancel()
		_ = cli.Close()
		return nil, fmt.Errorf("etcd: регистрация: %w", err)
	}
	rev, err := a.refresh(ctx, true)
	if err != nil {
		cancel()
		_ = cli.Close()
		return nil, fmt.Errorf("etcd: чтение членов: %w", err)
	}
	go a.run(rctx, ka, rev)
	return a, nil
}

// Checkpoints — хранилище чекпоинтов на том же соединении (закрывается в Close).
func (a *Assigner) Checkpoints() *CheckpointStore { return a.cps }

// register выдаёт lease и записывает ключ членства (значение содержит flight_addr).
func (a *Assigner) register(ctx context.Context) (<-chan *clientv3.LeaseKeepAliveResponse, error) {
	lg, err := a.cli.Grant(ctx, int64(a.opt.TTL/time.Second))
	if err != nil {
		return nil, err
	}
	val, _ := json.Marshal(a.opt.Self)
	if _, err := a.cli.Put(ctx, a.memKey, string(val), clientv3.WithLease(lg.ID)); err != nil {
		return nil, err
	}
	// keepalive не привязан к ctx вызова: он должен жить до Close или потери lease
	kctx, kcancel := context.WithCancel(context.Background())
	ka, err := a.cli.KeepAlive(kctx, lg.ID)
	if err != nil {
		kcancel()
		return nil, err
	}
	a.leaseMu.Lock()
	a.lease, a.stopKA = lg.ID, kcancel
	a.leaseMu.Unlock()
	return ka, nil
}

// refresh перечитывает членов. initial — старт: «прежние» члены = все, кроме себя,
// потому что кто-то из них уже может владеть устройствами и ещё не успел их отдать.
func (a *Assigner) refresh(ctx context.Context, initial bool) (int64, error) {
	resp, err := a.cli.Get(ctx, a.prefix+"collectors/", clientv3.WithPrefix())
	if err != nil {
		return 0, err
	}
	ids := make([]string, 0, len(resp.Kvs))
	for _, kv := range resp.Kvs {
		ids = append(ids, strings.TrimPrefix(string(kv.Key), a.prefix+"collectors/"))
	}
	sort.Strings(ids)
	a.mu.Lock()
	if initial {
		a.prev = Without(ids, a.opt.Self.CollectorID)
	} else {
		a.prev = a.members
	}
	a.members = ids
	a.dirty = true
	a.mu.Unlock()
	a.log.Info("члены etcd", "members", ids)
	return resp.Header.Revision, nil
}

// run: наблюдение за членами; при потере lease или обрыве watch — перерегистрация.
func (a *Assigner) run(ctx context.Context, ka <-chan *clientv3.LeaseKeepAliveResponse, rev int64) {
	defer close(a.done)
	for {
		err := a.watch(ctx, ka, rev)
		if ctx.Err() != nil {
			return
		}
		a.log.Warn("членство etcd нарушено, перерегистрация", "err", err)
		a.reregs.Add(1)
		a.leaseMu.Lock()
		a.stopKA()
		a.leaseMu.Unlock()
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
			}
			cctx, cancel := context.WithTimeout(ctx, 5*time.Second)
			var e error
			if ka, e = a.register(cctx); e == nil {
				rev, e = a.refresh(cctx, false)
			}
			cancel()
			if e == nil {
				break
			}
			a.log.Warn("перерегистрация не удалась", "err", e)
		}
	}
}

// watch возвращается при потере lease (канал keepalive закрыт) или обрыве watch.
func (a *Assigner) watch(ctx context.Context, ka <-chan *clientv3.LeaseKeepAliveResponse, rev int64) error {
	wctx, cancel := context.WithCancel(ctx)
	defer cancel()
	wch := a.cli.Watch(wctx, a.prefix+"collectors/", clientv3.WithPrefix(), clientv3.WithRev(rev+1))
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case _, ok := <-ka:
			if !ok {
				return errors.New("lease потерян")
			}
		case wr, ok := <-wch:
			if !ok {
				return errors.New("watch закрыт")
			}
			if err := wr.Err(); err != nil {
				return err
			}
			a.events.Add(1)
			// проще и надёжнее пересчитать по полному списку, чем накапливать события
			rctx, c := context.WithTimeout(ctx, 5*time.Second)
			_, err := a.refresh(rctx, false)
			c()
			if err != nil {
				return err
			}
		}
	}
}

// Devices возвращает устройства, которые можно опрашивать сейчас (дёшево: кэш).
func (a *Assigner) Devices(ctx context.Context) ([]string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.all) == 0 && a.opt.Devices != nil {
		l, err := a.opt.Devices(ctx)
		if err != nil {
			return nil, err
		}
		a.all = l
		a.dirty = true
	}
	now := a.opt.Now()
	if a.dirty {
		a.reassign(now)
	} else if !a.nextReady.IsZero() && !now.Before(a.nextReady) {
		a.rebuild(now)
	}
	return a.out, nil
}

// reassign пересчитывает владение по текущему списку членов. Вызывается под a.mu.
func (a *Assigner) reassign(now time.Time) {
	self := a.opt.Self.CollectorID
	mine := map[string]bool{}
	for _, d := range Assign(a.members, a.all, self) {
		mine[d] = true
	}
	// Освобождаемые устройства просто пропадают из списка: ядро перестаёт их опрашивать,
	// а уже прочитанное уходит в стоки и в чекпоинт ближайшим сбросом (FLUSH_INTERVAL).
	// Новый владелец ждёт Handover, поэтому «сначала сброс, потом передача» соблюдается.
	for d := range a.owned {
		if !mine[d] {
			delete(a.owned, d)
		}
	}
	for d := range mine {
		if _, ok := a.owned[d]; ok {
			continue
		}
		ready := now
		if p := Owner(a.prev, d); p != "" && p != self && contains(a.members, p) {
			ready = now.Add(a.opt.Handover) // прежний владелец жив и может держать данные
		}
		a.owned[d] = ready
	}
	a.dirty = false
	a.rebuild(now)
}

func (a *Assigner) rebuild(now time.Time) {
	out := make([]string, 0, len(a.owned))
	a.nextReady = time.Time{}
	for d, ready := range a.owned {
		if now.Before(ready) {
			if a.nextReady.IsZero() || ready.Before(a.nextReady) {
				a.nextReady = ready
			}
			continue
		}
		out = append(out, d)
	}
	sort.Strings(out)
	a.out = out
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

// Info — счётчики для /metrics и /status (см. collector/metrics.go).
func (a *Assigner) Info() map[string]int64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	waiting := 0
	now := a.opt.Now()
	for _, r := range a.owned {
		if now.Before(r) {
			waiting++
		}
	}
	return map[string]int64{
		"etcd_members":             int64(len(a.members)),
		"etcd_watch_events":        a.events.Load(),
		"etcd_reregistrations":     a.reregs.Load(),
		"devices_owned":            int64(len(a.owned)),
		"devices_handover_waiting": int64(waiting),
	}
}

// Members — id живых членов (копия).
func (a *Assigner) Members() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]string(nil), a.members...)
}

// Close отзывает lease (ключ членства исчезает сразу, остальные пересчитают HRW) и
// закрывает соединение. Ядро вызывает его уже после записи чекпоинтов.
func (a *Assigner) Close(ctx context.Context) error {
	a.cancel()
	<-a.done
	a.leaseMu.Lock()
	a.stopKA()
	lease := a.lease
	a.leaseMu.Unlock()
	_, err := a.cli.Revoke(ctx, lease)
	return errors.Join(err, a.cli.Close())
}
