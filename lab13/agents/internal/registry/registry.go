// Пакет registry — реестр видов агентов: kind из файла роли -> конструктор.
package registry

import (
	"fmt"
	"sort"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/booking"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/feedback"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/reminder"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/triage"
)

type Factory func(d core.Deps, params yaml.Node) (core.Instance, error)

type Entry struct {
	New Factory
	// RedisOptional: без REDIS_URL вид работает на хранилище в памяти (триаж).
	RedisOptional bool
}

func entries() map[string]Entry {
	return map[string]Entry{
		"triage": {RedisOptional: true, New: func(_ core.Deps, p yaml.Node) (core.Instance, error) {
			h, err := triage.New(p)
			if err != nil {
				return core.Instance{}, err
			}
			return core.Instance{Tasks: h}, nil
		}},
		"booking":  {New: booking.New},
		"reminder": {New: reminder.New},
		"feedback": {New: feedback.New},
	}
}

func Lookup(kind string) (Entry, bool) {
	e, ok := entries()[kind]
	return e, ok
}

// Kinds — отсортированный список допустимых видов.
func Kinds() []string {
	m := entries()
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func MustLookup(kind string) Entry {
	e, ok := Lookup(kind)
	if !ok {
		panic(fmt.Sprintf("неизвестный kind %q", kind))
	}
	return e
}
