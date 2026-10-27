// Пакет clock — инжектируемые часы: окна, сброс по таймеру и чекпоинты
// тестируются без time.Sleep.
package clock

import (
	"sort"
	"sync"
	"time"
)

type Ticker interface {
	C() <-chan time.Time
	Stop()
}

type Clock interface {
	Now() time.Time
	NewTicker(d time.Duration) Ticker
	After(d time.Duration) <-chan time.Time
}

// Real — системные часы (всегда UTC).
type Real struct{}

func (Real) Now() time.Time                         { return time.Now().UTC() }
func (Real) After(d time.Duration) <-chan time.Time { return time.After(d) }
func (Real) NewTicker(d time.Duration) Ticker       { return realTicker{time.NewTicker(d)} }

type realTicker struct{ t *time.Ticker }

func (r realTicker) C() <-chan time.Time { return r.t.C }
func (r realTicker) Stop()               { r.t.Stop() }

// Fake — ручные часы: время двигается только через Advance/Set.
type Fake struct {
	mu     sync.Mutex
	now    time.Time
	timers []*fakeTimer
}

type fakeTimer struct {
	when    time.Time
	period  time.Duration // 0 — одноразовый таймер
	ch      chan time.Time
	stopped bool
}

func NewFake(start time.Time) *Fake { return &Fake{now: start.UTC()} }

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *Fake) add(d, period time.Duration) *fakeTimer {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &fakeTimer{when: f.now.Add(d), period: period, ch: make(chan time.Time, 1)}
	f.timers = append(f.timers, t)
	return t
}

func (f *Fake) After(d time.Duration) <-chan time.Time { return f.add(d, 0).ch }

func (f *Fake) NewTicker(d time.Duration) Ticker {
	if d <= 0 {
		panic("clock: неположительный период тикера")
	}
	return &fakeTickerHandle{f: f, t: f.add(d, d)}
}

type fakeTickerHandle struct {
	f *Fake
	t *fakeTimer
}

func (h *fakeTickerHandle) C() <-chan time.Time { return h.t.ch }
func (h *fakeTickerHandle) Stop() {
	h.f.mu.Lock()
	h.t.stopped = true
	h.f.mu.Unlock()
}

// Waiters — число активных таймеров (чтобы тест дождался подписки горутины).
func (f *Fake) Waiters() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, t := range f.timers {
		if !t.stopped {
			n++
		}
	}
	return n
}

// Advance двигает время вперёд, по порядку срабатывая таймеры.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	target := f.now.Add(d)
	for {
		sort.SliceStable(f.timers, func(i, j int) bool { return f.timers[i].when.Before(f.timers[j].when) })
		var next *fakeTimer
		for _, t := range f.timers {
			if !t.stopped && !t.when.After(target) {
				next = t
				break
			}
		}
		if next == nil {
			break
		}
		f.now = next.when
		select {
		case next.ch <- next.when:
		default: // как у time.Ticker: медленный получатель пропускает тики
		}
		if next.period > 0 {
			next.when = next.when.Add(next.period)
		} else {
			next.stopped = true
		}
	}
	f.now = target
	live := f.timers[:0]
	for _, t := range f.timers {
		if !t.stopped {
			live = append(live, t)
		}
	}
	f.timers = live
	f.mu.Unlock()
}
