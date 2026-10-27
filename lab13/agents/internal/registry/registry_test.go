package registry

import (
	"io"
	"log/slog"
	"reflect"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

func TestKindsAreSortedAndComplete(t *testing.T) {
	t.Parallel()
	want := []string{"booking", "feedback", "reminder", "triage"}
	if got := Kinds(); !reflect.DeepEqual(got, want) {
		t.Fatalf("Kinds = %v, ожидалось %v", got, want)
	}
}

func TestLookupAndMust(t *testing.T) {
	t.Parallel()
	if e, ok := Lookup("triage"); !ok || !e.RedisOptional || e.New == nil {
		t.Fatalf("triage: %+v ok=%v", e, ok)
	}
	if e, ok := Lookup("booking"); !ok || e.RedisOptional {
		t.Fatalf("booking требует Redis: %+v", e)
	}
	if _, ok := Lookup("intake"); ok {
		t.Fatal("intake — Python-вид, в Go-реестре его нет")
	}
	if MustLookup("reminder").New == nil {
		t.Fatal("MustLookup вернул пустой конструктор")
	}
}

func TestMustLookupPanicsOnUnknown(t *testing.T) {
	t.Parallel()
	defer func() {
		if recover() == nil {
			t.Fatal("ожидалась паника для неизвестного вида")
		}
	}()
	MustLookup("nope")
}

// Каждый вид собирается из минимальных параметров (так же, как в файле роли).
func TestEntryFactoriesBuild(t *testing.T) {
	t.Parallel()
	params := map[string]string{
		"triage":   "emergency_phrases: [обморок]\n",
		"booking":  "doctors:\n  - id: d-1\n    specialty: therapist\n    work_days: [1]\n    work_start: \"08:00\"\n    work_end: \"14:00\"\n    slot_minutes: 20\n",
		"reminder": "offsets: [24h]\n",
		"feedback": "survey_delay: 2h\n",
	}
	for kind, src := range params {
		var node yaml.Node
		if err := yaml.Unmarshal([]byte(src), &node); err != nil {
			t.Fatal(err)
		}
		e := MustLookup(kind)
		deps := core.Deps{AgentID: kind + "-1", Step: kind, Store: store.NewMemory(), Clock: time.Now,
			Loc: time.UTC, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
		inst, err := e.New(deps, *node.Content[0])
		if err != nil {
			t.Fatalf("%s: %v", kind, err)
		}
		if kind == "booking" && len(inst.Subscriptions) != 2 {
			t.Errorf("booking: подписок %d", len(inst.Subscriptions))
		}
		if kind == "triage" && inst.Tasks == nil {
			t.Errorf("triage: нет обработчика задач")
		}
	}
}
