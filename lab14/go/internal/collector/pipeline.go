package collector

import (
	"errors"
	"time"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

const flushRetryDelay = time.Second

type pipeline struct {
	c       *Collector
	raw     []model.Reading
	agg     []model.Agg
	pending map[string]int64 // максимальный seq по устройству, ещё не подтверждённый чекпоинтом
	emitRaw bool
	emitAgg bool
}

// runPipeline — единственный потребитель канала: окна, накопление пакета, сброс.
// Возвращается после закрытия канала и финального сброса.
func (c *Collector) runPipeline() error {
	p := &pipeline{c: c, pending: map[string]int64{},
		emitRaw: c.cfg.Emit != "agg", emitAgg: c.cfg.Emit != "raw"}
	flushT := c.clk.NewTicker(c.cfg.FlushInterval)
	defer flushT.Stop()
	winT := c.clk.NewTicker(windowTick)
	defer winT.Stop()

	var firstErr error
	fail := func(err error) {
		if firstErr == nil {
			firstErr = err
		}
	}
	for {
		select {
		case r, ok := <-c.in:
			if !ok {
				return errors.Join(firstErr, p.finish())
			}
			if firstErr != nil {
				continue // сбросить не удалось окончательно — только освобождаем канал
			}
			if err := p.handle(r); err != nil && c.abort.Err() != nil {
				fail(err) // иначе (чекпоинт не записан) повторим со следующим сбросом
			}
		case <-flushT.C():
			if firstErr == nil {
				if err := p.flush(); err != nil && c.abort.Err() != nil {
					fail(err)
				}
			}
		case <-winT.C():
			if firstErr == nil && c.cfg.Emit != "raw" {
				p.addAgg(c.agg.CloseDue(c.clk.Now()))
			}
		}
	}
}

func (p *pipeline) handle(r model.Reading) error {
	c := p.c
	if r.Seq > p.pending[r.DeviceID] {
		p.pending[r.DeviceID] = r.Seq
	}
	if p.emitAgg {
		// опоздавшее не теряется молча: счётчик late_dropped (в /metrics)
		c.agg.Add(r, c.clk.Now())
	}
	if p.emitRaw {
		p.raw = append(p.raw, r)
	}
	if len(p.raw)+len(p.agg) >= c.cfg.BatchSize {
		return p.flush()
	}
	return nil
}

func (p *pipeline) addAgg(a []model.Agg) {
	if p.emitAgg {
		p.agg = append(p.agg, a...)
	}
}

// finish: закрыть все окна (досрочно — с пометкой в логе), сбросить, записать чекпоинты.
func (p *pipeline) finish() error {
	c := p.c
	if p.emitAgg {
		before := c.agg.Stats().EarlyClosed
		aggs := c.agg.CloseAll(c.clk.Now())
		c.log.Info("остановка: шаг 3 — окна закрыты", "windows", len(aggs),
			"early_closed", c.agg.Stats().EarlyClosed-before, "reason", "shutdown")
		p.addAgg(aggs)
	}
	c.log.Info("остановка: шаги 4–5 — сброс стоков и чекпоинты", "raw", len(p.raw), "agg", len(p.agg))
	return p.flush()
}

// flush: записать накопленное во все стоки, Flush всех стоков, затем (только после
// успеха) чекпоинты. Неудавшиеся стоки повторяются, удавшиеся повторно не пишутся.
// Повторы идут до успеха, а при остановке — до истечения SHUTDOWN_TIMEOUT.
func (p *pipeline) flush() error {
	c := p.c
	if len(p.raw) == 0 && len(p.agg) == 0 && len(p.pending) == 0 {
		return nil
	}
	const (
		todo = iota
		written
		flushed
	)
	stage := make([]int, len(c.sinks))
	for {
		var failed []error
		for i, s := range c.sinks {
			var err error
			if stage[i] == todo {
				if p.emitRaw && len(p.raw) > 0 {
					err = s.Sink.WriteRaw(c.abort, p.raw)
				}
				if err == nil && p.emitAgg && len(p.agg) > 0 {
					err = s.Sink.WriteAgg(c.abort, p.agg)
				}
				if err == nil {
					stage[i] = written
				}
			}
			if err == nil && stage[i] == written {
				if err = s.Sink.Flush(c.abort); err == nil {
					stage[i] = flushed
				}
			}
			if err != nil {
				c.m.errors.Add(1)
				c.m.flushErrors.Add(1)
				c.log.Error("ошибка стока", "sink", s.Name, "err", err)
				failed = append(failed, err)
			}
		}
		if len(failed) == 0 {
			break
		}
		select {
		case <-c.clk.After(flushRetryDelay):
		case <-c.abort.Done():
			return errors.Join(failed...)
		}
	}
	c.m.flushes.Add(1)
	c.m.batchRecords.Add(int64(len(p.raw) + len(p.agg)))
	p.raw, p.agg = p.raw[:0], p.agg[:0]

	if len(p.pending) > 0 {
		if err := c.cps.Save(c.abort, p.pending); err != nil {
			// записанные данные не потеряны; чекпоинт повторим со следующим сбросом
			c.m.errors.Add(1)
			c.log.Error("не удалось записать чекпоинты", "err", err)
			return err
		}
		c.m.checkpointSaves.Add(1)
		clear(p.pending)
	}
	return nil
}
