package collector

import (
	"context"
	"sync"
)

// startPolling запускает диспетчер и пул воркеров. Горутины завершаются при
// отмене ctx; wg.Wait() возвращается, когда больше никто не пишет в c.in.
func (c *Collector) startPolling(ctx context.Context, wg *sync.WaitGroup) {
	jobs := make(chan string, c.cfg.Workers)
	for i := 0; i < c.cfg.Workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for dev := range jobs {
				c.pollDevice(ctx, dev)
				c.curMu.Lock()
				delete(c.flight, dev)
				c.curMu.Unlock()
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		defer close(jobs)
		t := c.clk.NewTicker(c.cfg.PollInterval)
		defer t.Stop()
		for {
			c.dispatch(ctx, jobs)
			select {
			case <-ctx.Done():
				return
			case <-t.C():
			}
		}
	}()
}

func (c *Collector) dispatch(ctx context.Context, jobs chan<- string) {
	devs, err := c.asg.Devices(ctx)
	if err != nil {
		if ctx.Err() == nil {
			c.m.errors.Add(1)
			c.log.Warn("не удалось получить список устройств", "err", err)
		}
		return
	}
	c.devices.Store(&devs)
	for _, dev := range devs {
		c.curMu.Lock()
		busy := c.flight[dev]
		if !busy {
			c.flight[dev] = true
		}
		c.curMu.Unlock()
		if busy {
			continue // предыдущий опрос ещё идёт
		}
		select {
		case jobs <- dev:
		case <-ctx.Done():
			c.curMu.Lock()
			delete(c.flight, dev)
			c.curMu.Unlock()
			return
		}
	}
}

func (c *Collector) cursor(ctx context.Context, dev string) (int64, bool) {
	c.curMu.Lock()
	cur, ok := c.cursors[dev]
	c.curMu.Unlock()
	if ok {
		return cur, true
	}
	cp, err := c.cps.Load(ctx, dev)
	if err != nil {
		c.m.errors.Add(1)
		c.log.Warn("не удалось прочитать чекпоинт", "device", dev, "err", err)
		return 0, false
	}
	c.curMu.Lock()
	c.cursors[dev] = cp
	c.curMu.Unlock()
	return cp, true
}

// pollDevice вычитывает всё новое устройства (в несколько запросов, если накопилось
// больше FETCH_LIMIT). Курсор двигается только после того, как все показания ответа
// положены в канал.
func (c *Collector) pollDevice(ctx context.Context, dev string) {
	cur, ok := c.cursor(ctx, dev)
	if !ok {
		return
	}
	for ctx.Err() == nil {
		resp, err := c.src.Readings(ctx, dev, cur, c.cfg.FetchLimit)
		c.m.polls.Add(1)
		if err != nil {
			if ctx.Err() == nil {
				c.m.pollErrors.Add(1)
				c.m.errors.Add(1)
				c.log.Warn("ошибка опроса", "device", dev, "err", err)
			}
			return
		}
		for _, r := range resp.Readings {
			c.in <- r // pipeline читает до закрытия канала, взаимной блокировки нет
		}
		c.m.readings.Add(int64(len(resp.Readings)))
		prev := cur
		if resp.LastSeq > cur {
			cur = resp.LastSeq
			c.curMu.Lock()
			c.cursors[dev] = cur
			c.curMu.Unlock()
		}
		if cur-prev < int64(c.cfg.FetchLimit) {
			return // догнали
		}
	}
}
