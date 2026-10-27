package auth

import (
	"sync"
	"testing"
	"time"
)

func TestLoginLimiter(t *testing.T) {
	l := NewLoginLimiter(3, 5*time.Minute)
	now := time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
	l.now = func() time.Time { return now }

	for i := range 3 {
		if ok, _ := l.Allow("alice|1.2.3.4"); !ok {
			t.Fatalf("попытка %d заблокирована раньше времени", i+1)
		}
		l.Failure("alice|1.2.3.4")
	}
	ok, retry := l.Allow("alice|1.2.3.4")
	if ok || retry != 5*time.Minute {
		t.Fatalf("после 3 неудач: ok=%v retry=%v", ok, retry)
	}
	if ok, _ := l.Allow("alice|5.6.7.8"); !ok {
		t.Fatal("другой IP не должен блокироваться")
	}

	now = now.Add(2 * time.Minute)
	if _, retry := l.Allow("alice|1.2.3.4"); retry != 3*time.Minute {
		t.Fatalf("Retry-After должен уменьшаться: %v", retry)
	}
	now = now.Add(3 * time.Minute)
	if ok, _ := l.Allow("alice|1.2.3.4"); !ok {
		t.Fatal("после окна блокировка должна сниматься")
	}
}

func TestLoginLimiterSuccessResets(t *testing.T) {
	l := NewLoginLimiter(2, time.Minute)
	l.Failure("k")
	l.Success("k")
	l.Failure("k")
	if ok, _ := l.Allow("k"); !ok {
		t.Fatal("успешный вход должен сбрасывать счётчик")
	}
}

func TestLoginLimiterConcurrent(t *testing.T) {
	l := NewLoginLimiter(1000, time.Minute)
	var wg sync.WaitGroup
	for range 100 {
		wg.Go(func() {
			l.Allow("k")
			l.Failure("k")
		})
	}
	wg.Wait()
	if l.entries["k"].failures != 100 {
		t.Fatalf("failures = %d", l.entries["k"].failures)
	}
}
