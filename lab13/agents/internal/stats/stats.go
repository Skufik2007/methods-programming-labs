// Пакет stats — счётчики processed/failed. В Redis хранятся в mas:stats:<step>:
// агрегат по шагу (processed, failed) и персональные поля processed:<agent_id>,
// failed:<agent_id> — их после рестарта восстанавливает экземпляр с тем же AGENT_ID.
package stats

import (
	"context"
	"log/slog"
	"strconv"
	"sync/atomic"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

func Key(step string) string { return "mas:stats:" + step }

type Counters struct {
	st        store.Store
	key       string
	agent     string
	log       *slog.Logger
	processed atomic.Int64
	failed    atomic.Int64
}

func New(st store.Store, step, agentID string, log *slog.Logger) *Counters {
	return &Counters{st: st, key: Key(step), agent: agentID, log: log}
}

// Load подхватывает сохранённые значения; ошибка Redis не фатальна.
func (c *Counters) Load(ctx context.Context) {
	m, err := c.st.HGetAll(ctx, c.key)
	if err != nil {
		c.log.Warn("не удалось прочитать счётчики", "err", err)
		return
	}
	p, _ := strconv.ParseInt(m["processed:"+c.agent], 10, 64)
	f, _ := strconv.ParseInt(m["failed:"+c.agent], 10, 64)
	c.processed.Store(p)
	c.failed.Store(f)
}

// Done учитывает одну обработанную задачу/сообщение.
func (c *Counters) Done(ctx context.Context, failed bool) {
	c.processed.Add(1)
	c.incr(ctx, "processed")
	if failed {
		c.failed.Add(1)
		c.incr(ctx, "failed")
	}
}

func (c *Counters) incr(ctx context.Context, field string) {
	for _, f := range []string{field, field + ":" + c.agent} {
		if _, err := c.st.HIncrBy(ctx, c.key, f, 1); err != nil {
			c.log.Warn("не удалось обновить счётчик", "field", f, "err", err)
			return
		}
	}
}

func (c *Counters) Processed() int64 { return c.processed.Load() }
func (c *Counters) Failed() int64    { return c.failed.Load() }
