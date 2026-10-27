package orders

import (
	"errors"
	"sync"
	"testing"
)

func TestStoreOwnership(t *testing.T) {
	s := NewStore()
	o := s.Create("alice", validRequest())

	if o.TotalCents != 2*19999+1000 || o.Total != 409.98 {
		t.Fatalf("сумма %d / %v", o.TotalCents, o.Total)
	}
	if got, err := s.Get("alice", o.ID); err != nil || got.ID != o.ID {
		t.Fatalf("Get своего заказа: %v", err)
	}
	if _, err := s.Get("bob", o.ID); !errors.Is(err, ErrForbidden) {
		t.Fatalf("чужой заказ: %v", err)
	}
	if _, err := s.Get("alice", "nope"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("несуществующий заказ: %v", err)
	}
	if len(s.List("bob")) != 0 || len(s.List("alice")) != 1 {
		t.Fatal("List не фильтрует по владельцу")
	}
}

func TestStoreConcurrent(t *testing.T) {
	s := NewStore()
	var wg sync.WaitGroup
	for range 50 {
		wg.Go(func() {
			s.Create("alice", validRequest())
			s.List("alice")
		})
	}
	wg.Wait()
	if n := len(s.List("alice")); n != 50 {
		t.Fatalf("заказов %d, want 50", n)
	}
}
