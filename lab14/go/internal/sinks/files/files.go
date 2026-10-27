// Пакет files — сток JSONL: OUT_DIR/raw|agg/<collector_id>-<YYYYMMDDHH>.jsonl
// (час — по часам сборщика в момент записи).
package files

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync/atomic"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/sinks"
)

type out struct {
	hour string
	f    *os.File
	w    *bufio.Writer
}

type Sink struct {
	dir, id string
	clk     clock.Clock
	raw     out
	agg     out
	buf     []byte

	rawBytes, aggBytes, rawRecs, aggRecs atomic.Int64
}

func New(outDir, collectorID string, clk clock.Clock) *Sink {
	return &Sink{dir: outDir, id: collectorID, clk: clk}
}

// Factory — для sinks.Registry.
func Factory(env sinks.Env) (sinks.Sink, error) {
	return New(env.Config.OutDir, env.Config.CollectorID, env.Clock), nil
}

func (s *Sink) open(o *out, kind string) error {
	hour := s.clk.Now().UTC().Format("2006010215")
	if o.f != nil && o.hour == hour {
		return nil
	}
	if o.f != nil { // смена часа: закрыть предыдущий файл
		if err := o.close(); err != nil {
			return err
		}
	}
	dir := filepath.Join(s.dir, kind)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(dir, fmt.Sprintf("%s-%s.jsonl", s.id, hour)), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	*o = out{hour: hour, f: f, w: bufio.NewWriterSize(f, 256<<10)}
	return nil
}

func (o *out) flush() error {
	if o.f == nil {
		return nil
	}
	if err := o.w.Flush(); err != nil {
		return err
	}
	return o.f.Sync() // чекпоинт двигается только после Flush — нужна долговечность
}

func (o *out) close() error {
	if o.f == nil {
		return nil
	}
	err := errors.Join(o.flush(), o.f.Close())
	*o = out{}
	return err
}

func (s *Sink) WriteRaw(_ context.Context, batch []model.Reading) error {
	if len(batch) == 0 {
		return nil
	}
	if err := s.open(&s.raw, "raw"); err != nil {
		return err
	}
	for _, r := range batch {
		s.buf = append(r.AppendJSON(s.buf[:0]), '\n')
		n, err := s.raw.w.Write(s.buf)
		s.rawBytes.Add(int64(n))
		if err != nil {
			return err
		}
		s.rawRecs.Add(1)
	}
	return nil
}

func (s *Sink) WriteAgg(_ context.Context, batch []model.Agg) error {
	if len(batch) == 0 {
		return nil
	}
	if err := s.open(&s.agg, "agg"); err != nil {
		return err
	}
	for _, a := range batch {
		s.buf = append(a.AppendJSON(s.buf[:0]), '\n')
		n, err := s.agg.w.Write(s.buf)
		s.aggBytes.Add(int64(n))
		if err != nil {
			return err
		}
		s.aggRecs.Add(1)
	}
	return nil
}

func (s *Sink) Flush(context.Context) error { return errors.Join(s.raw.flush(), s.agg.flush()) }

func (s *Sink) Close() error { return errors.Join(s.raw.close(), s.agg.close()) }

func (s *Sink) Stats() sinks.Stats {
	return sinks.Stats{RawBytes: s.rawBytes.Load(), AggBytes: s.aggBytes.Load(),
		RawRecords: s.rawRecs.Load(), AggRecords: s.aggRecs.Load()}
}
