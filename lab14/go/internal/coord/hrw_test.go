package coord

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"
)

func devices(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("dev-%04d", i+1)
	}
	return out
}

func TestOwnerDeterministicAcrossMemberOrder(t *testing.T) {
	members := []string{"c1", "c2", "c3", "c4"}
	for _, d := range devices(200) {
		want := Owner(members, d)
		for i := 0; i < 5; i++ {
			shuffled := slices.Clone(members)
			rand.Shuffle(len(shuffled), func(a, b int) { shuffled[a], shuffled[b] = shuffled[b], shuffled[a] })
			if got := Owner(shuffled, d); got != want {
				t.Fatalf("устройство %s: владелец зависит от порядка списка (%s и %s)", d, want, got)
			}
		}
	}
}

func TestOwnerEmptyMembers(t *testing.T) {
	if got := Owner(nil, "dev-0001"); got != "" {
		t.Fatalf("для пустого списка владелец %q, ожидалась пустая строка", got)
	}
}

func TestUniformityTwoMembers(t *testing.T) {
	members := []string{"c1", "c2"}
	devs := devices(1000)
	count := map[string]int{}
	for _, d := range devs {
		count[Owner(members, d)]++
	}
	for _, m := range members {
		share := float64(count[m]) / float64(len(devs))
		if share < 0.35 {
			t.Fatalf("сборщик %s получил %.1f%% устройств, ожидалось не менее 35%% (распределение %v)", m, 100*share, count)
		}
	}
}

// Добавление члена перемещает только устройства, которые переходят к новому члену;
// перераспределения между старыми членами нет. Доля переехавших ≈ 1/3.
func TestMinimalMovementOnMemberAdded(t *testing.T) {
	before := []string{"c1", "c2"}
	after := []string{"c1", "c2", "c3"}
	devs := devices(3000)
	moved := 0
	for _, d := range devs {
		a, b := Owner(before, d), Owner(after, d)
		if a == b {
			continue
		}
		moved++
		if b != "c3" {
			t.Fatalf("устройство %s переехало %s -> %s: должно уходить только к новому члену", d, a, b)
		}
	}
	share := float64(moved) / float64(len(devs))
	if share < 0.25 || share > 0.42 {
		t.Fatalf("переехало %.1f%% устройств, ожидалось около 33%%", 100*share)
	}
}

func TestAssign(t *testing.T) {
	devs := devices(50)
	members := []string{"c1", "c2"}

	t.Run("partition covers all devices without overlap", func(t *testing.T) {
		a := Assign(members, devs, "c1")
		b := Assign(members, devs, "c2")
		if len(a)+len(b) != len(devs) {
			t.Fatalf("%d + %d != %d", len(a), len(b), len(devs))
		}
		for _, d := range a {
			if slices.Contains(b, d) {
				t.Fatalf("устройство %s у двух сборщиков", d)
			}
		}
		if !slices.IsSorted(a) {
			t.Fatal("список устройств не отсортирован")
		}
	})

	t.Run("self not registered owns nothing", func(t *testing.T) {
		if got := Assign(members, devs, "c9"); got != nil {
			t.Fatalf("незарегистрированный сборщик получил %d устройств", len(got))
		}
	})
}

func TestWithout(t *testing.T) {
	got := Without([]string{"c1", "c2", "c3"}, "c2")
	if !slices.Equal(got, []string{"c1", "c3"}) {
		t.Fatalf("Without = %v", got)
	}
}
