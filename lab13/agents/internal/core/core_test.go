package core

import (
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func TestClockUTCAndNil(t *testing.T) {
	t.Parallel()
	var nilClock Clock
	if nilClock.Now().Location() != time.UTC {
		t.Fatal("nil-часы должны давать UTC")
	}
	msk := time.FixedZone("MSK", 3*3600)
	c := Clock(func() time.Time { return time.Date(2026, 10, 13, 10, 0, 0, 0, msk) })
	if got := c.Now(); got.Location() != time.UTC || got.Hour() != 7 {
		t.Fatalf("часы должны приводиться к UTC: %v", got)
	}
}

func TestFailureClassification(t *testing.T) {
	t.Parallel()
	perm := Permanent("плохо: %d", 1)
	if IsRetryable(perm) || perm.Error() != "плохо: 1" {
		t.Errorf("Permanent: retryable=%v msg=%q", IsRetryable(perm), perm.Error())
	}
	tr := Transient("сеть %s", "x")
	if !IsRetryable(tr) {
		t.Error("Transient должен быть повторяемым")
	}
	// неизвестные ошибки считаем временными
	if !IsRetryable(errors.New("что-то")) {
		t.Error("неизвестная ошибка — повторяемая")
	}
	// обёрнутая постоянная ошибка сохраняет признак через errors.As
	if IsRetryable(fmtWrap{perm}) {
		t.Error("обёрнутая Permanent остаётся постоянной")
	}
}

type fmtWrap struct{ inner error }

func (w fmtWrap) Error() string { return "обёртка: " + w.inner.Error() }
func (w fmtWrap) Unwrap() error { return w.inner }

func TestStableID(t *testing.T) {
	t.Parallel()
	a := StableID("reminder", "appt-1", "0")
	b := StableID("reminder", "appt-1", "0")
	c := StableID("reminder", "appt-1", "1")
	if a != b || a == c {
		t.Fatalf("StableID: a=%s b=%s c=%s", a, b, c)
	}
	if len(a) != 36 || a[14] != '5' {
		t.Errorf("ожидался UUID v5, получено %s", a)
	}
}

func TestParsePayloadLayers(t *testing.T) {
	t.Parallel()
	raw := json.RawMessage(`{"top":1,"case":{"patient_id":"p","age":34},"previous":{"specialty":"ent","top":"prev"}}`)
	p, err := ParsePayload(raw)
	if err != nil {
		t.Fatal(err)
	}
	var pid string
	if !p.Get("patient_id", &pid, 1) || pid != "p" {
		t.Fatalf("patient_id из case: %q", pid)
	}
	var top int
	if !p.Get("top", &top) || top != 1 {
		t.Fatalf("по умолчанию порядок 0,1,2: top=%d", top)
	}
	var spec string
	if !p.Get("specialty", &spec) || spec != "ent" {
		t.Fatalf("specialty из previous: %q", spec)
	}
	// порядок задан явно: previous раньше верхнего уровня
	var fromPrev string
	if !p.Get("top", &fromPrev, 2, 0) || fromPrev != "prev" {
		t.Fatalf("явный порядок: %q", fromPrev)
	}
	var missing string
	if p.Get("нет-такого", &missing) {
		t.Fatal("отсутствующий ключ не должен находиться")
	}
	if p.Layer(-1) != nil || p.Layer(5) != nil {
		t.Error("выход за границы слоёв — nil")
	}
}

func TestParsePayloadEdgeCases(t *testing.T) {
	t.Parallel()
	empty, err := ParsePayload(json.RawMessage("   "))
	if err != nil {
		t.Fatalf("пустой payload: %v", err)
	}
	var v int
	if empty.Get("x", &v) {
		t.Error("в пустом payload ничего нет")
	}
	if _, err := ParsePayload(json.RawMessage(`[1,2]`)); err == nil {
		t.Error("массив вместо объекта — ошибка")
	}
	// case не объект: слой пустой
	p, _ := ParsePayload(json.RawMessage(`{"case":"строка","k":null}`))
	if p.Layer(1) != nil {
		t.Error("case-строка не должна становиться слоем")
	}
	var k int
	if p.Get("k", &k) {
		t.Error("null не считается значением")
	}
	// значение неподходящего типа пропускается, поиск идёт дальше
	p2, _ := ParsePayload(json.RawMessage(`{"rating":"пять","case":{"rating":5}}`))
	var r int
	if !p2.Get("rating", &r, 0, 1) || r != 5 {
		t.Fatalf("должен найтись корректный тип в case: %d", r)
	}
}
