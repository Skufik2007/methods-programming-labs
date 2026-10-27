package api

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"testing"
)

// Бенчмарки обработчиков без сети: показывают стоимость самого Gin
// (маршрутизация, middleware, разбор JSON, валидация) и число аллокаций.
//
//	go test -bench . -benchmem ./internal/api/

func BenchmarkPing(b *testing.B) {
	e := newEnv(b)
	b.ReportAllocs()
	for b.Loop() {
		w := httptest.NewRecorder()
		e.router.ServeHTTP(w, httptest.NewRequest("GET", "/ping", nil))
		if w.Code != 200 {
			b.Fatal(w.Code)
		}
	}
}

func BenchmarkValidateOrder(b *testing.B) {
	e := newEnv(b)
	body, _ := json.Marshal(orderBody())
	b.ReportAllocs()
	for b.Loop() {
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/api/v1/orders/validate", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		e.router.ServeHTTP(w, req)
		if w.Code != 200 {
			b.Fatal(w.Code, w.Body.String())
		}
	}
}
