// Пакет sinks — интерфейс стока и реестр фабрик. Новые стоки (nats, flight) только
// регистрируются в Registry в cmd/collector; ядро сборщика не меняется.
package sinks

import (
	"context"
	"fmt"
	"log/slog"
	"sort"

	"github.com/Skufik2007/methods-programming-lab14/go/internal/clock"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/config"
	"github.com/Skufik2007/methods-programming-lab14/go/internal/model"
)

// Sink принимает пакеты. Write* могут буферизовать; данные считаются записанными
// после успешного Flush — только тогда ядро двигает чекпоинты. Методы Write* и
// Flush вызываются из одной горутины, Stats — из любой.
type Sink interface {
	WriteRaw(ctx context.Context, batch []model.Reading) error
	WriteAgg(ctx context.Context, batch []model.Agg) error
	Flush(ctx context.Context) error
	Close() error
}

// Stats — счётчики стока для /metrics и /status.
type Stats struct {
	RawBytes, AggBytes, RawRecords, AggRecords int64
}

// StatsReporter — необязательный интерфейс стока.
type StatsReporter interface{ Stats() Stats }

// Named — сток с именем из SINKS.
type Named struct {
	Name string
	Sink Sink
}

// Env — всё, что нужно фабрике стока. Подключения создаются в фабрике, не при импорте.
type Env struct {
	Config config.Collector
	Clock  clock.Clock
	Log    *slog.Logger
}

type Factory func(env Env) (Sink, error)

type Registry struct{ factories map[string]Factory }

func NewRegistry() *Registry { return &Registry{factories: map[string]Factory{}} }

func (r *Registry) Register(name string, f Factory) { r.factories[name] = f }

func (r *Registry) Names() []string {
	var out []string
	for n := range r.factories {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// Build создаёт стоки по именам; при ошибке закрывает уже созданные.
func (r *Registry) Build(names []string, env Env) ([]Named, error) {
	var out []Named
	for _, n := range names {
		f, ok := r.factories[n]
		if !ok {
			closeAll(out)
			return nil, fmt.Errorf("сток %q не зарегистрирован (есть: %v)", n, r.Names())
		}
		s, err := f(env)
		if err != nil {
			closeAll(out)
			return nil, fmt.Errorf("сток %q: %w", n, err)
		}
		out = append(out, Named{n, s})
	}
	return out, nil
}

func closeAll(s []Named) {
	for _, n := range s {
		_ = n.Sink.Close()
	}
}
