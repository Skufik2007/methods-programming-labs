package api

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"lab10/internal/auth"
	"lab10/internal/orders"
)

type testEnv struct {
	t      testing.TB
	router *gin.Engine
	ready  *atomic.Bool
}

func newEnv(t testing.TB) *testEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	issuer, err := auth.NewIssuer(auth.Config{Issuer: "test", Audience: "lab10", TTL: time.Minute}, key)
	if err != nil {
		t.Fatal(err)
	}
	users, err := auth.NewUsers([]auth.UserSpec{
		{Name: "alice", Password: "alice-pass-1", Role: "user"},
		{Name: "bob", Password: "bob-pass-12", Role: "user"},
	})
	if err != nil {
		t.Fatal(err)
	}
	ready := &atomic.Bool{}
	ready.Store(true)
	r, err := NewRouter(Deps{
		Issuer: issuer, Users: users, Store: orders.NewStore(), Ready: ready,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &testEnv{t: t, router: r, ready: ready}
}

func (e *testEnv) do(method, path, token string, body any) *httptest.ResponseRecorder {
	e.t.Helper()
	var r io.Reader
	switch b := body.(type) {
	case nil:
	case string:
		r = strings.NewReader(b)
	default:
		data, _ := json.Marshal(b)
		r = bytes.NewReader(data)
	}
	req := httptest.NewRequest(method, path, r)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	return w
}

func (e *testEnv) login(user, pass string) string {
	e.t.Helper()
	w := e.do("POST", "/auth/login", "", map[string]string{"username": user, "password": pass})
	if w.Code != 200 {
		e.t.Fatalf("login %s: %d %s", user, w.Code, w.Body)
	}
	var tr TokenResponse
	if err := json.Unmarshal(w.Body.Bytes(), &tr); err != nil {
		e.t.Fatal(err)
	}
	if tr.TokenType != "Bearer" || tr.ExpiresIn <= 0 {
		e.t.Fatalf("token response %+v", tr)
	}
	return tr.AccessToken
}

func orderBody() map[string]any {
	return map[string]any{
		"customer": map[string]any{"name": "Иван Петров", "email": "ivan@example.com"},
		"items": []map[string]any{
			{"sku": "ABC-123", "quantity": 2, "price": 199.99},
			{"sku": "DEF-4567", "quantity": 1, "price": 50},
		},
		"delivery": map[string]any{"address": "Москва, ул. Пушкина, 1", "date": time.Now().AddDate(0, 0, 3).Format(time.DateOnly)},
	}
}

func decodeError(t *testing.T, w *httptest.ResponseRecorder) ErrorBody {
	t.Helper()
	var resp struct {
		Error ErrorBody `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("ответ не JSON: %s", w.Body)
	}
	return resp.Error
}

func TestOrderFlow(t *testing.T) {
	e := newEnv(t)
	alice := e.login("alice", "alice-pass-1")

	w := e.do("POST", "/api/v1/orders", alice, orderBody())
	if w.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", w.Code, w.Body)
	}
	var o orders.Order
	_ = json.Unmarshal(w.Body.Bytes(), &o)
	if o.TotalCents != 44998 || o.Owner != "alice" || w.Header().Get("Location") != "/api/v1/orders/"+o.ID {
		t.Fatalf("order %+v location %q", o, w.Header().Get("Location"))
	}

	if w := e.do("GET", "/api/v1/orders/"+o.ID, alice, nil); w.Code != 200 {
		t.Fatalf("get: %d", w.Code)
	}
	w = e.do("GET", "/api/v1/orders", alice, nil)
	var list struct{ Count int }
	_ = json.Unmarshal(w.Body.Bytes(), &list)
	if list.Count != 1 {
		t.Fatalf("list count %d", list.Count)
	}

	bob := e.login("bob", "bob-pass-12")
	// Чужой заказ неотличим от несуществующего, чтобы не раскрывать чужие id.
	if w := e.do("GET", "/api/v1/orders/"+o.ID, bob, nil); w.Code != http.StatusNotFound {
		t.Fatalf("чужой заказ: %d", w.Code)
	}
	if w := e.do("GET", "/api/v1/orders/00000000-0000-0000-0000-000000000000", alice, nil); w.Code != 404 {
		t.Fatalf("несуществующий: %d", w.Code)
	}
	if w := e.do("GET", "/api/v1/orders/not-a-uuid", alice, nil); w.Code != 400 {
		t.Fatalf("плохой id: %d", w.Code)
	}
}

func TestValidationErrors(t *testing.T) {
	e := newEnv(t)
	body := orderBody()
	body["items"] = []map[string]any{
		{"sku": "bad", "quantity": 0, "price": 1.999},
		{"sku": "bad", "quantity": 1, "price": 1},
	}
	body["customer"] = map[string]any{"name": "И", "email": "nope"}

	w := e.do("POST", "/api/v1/orders/validate", "", body)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status %d %s", w.Code, w.Body)
	}
	got := map[string]string{}
	for _, d := range decodeError(t, w).Details {
		got[d.Field] = d.Rule
	}
	want := map[string]string{
		"customer.name": "min", "customer.email": "email",
		"items[0].sku": "sku", "items[0].quantity": "min", "items[0].price": "money",
		"items[1].sku": "sku",
	}
	for f, rule := range want {
		if got[f] != rule {
			t.Errorf("поле %s: правило %q, want %q (все: %v)", f, got[f], rule, got)
		}
	}
}

func TestBadRequests(t *testing.T) {
	e := newEnv(t)
	cases := []struct {
		name   string
		body   string
		status int
		code   string
	}{
		{"пустое тело", "", 400, "empty_body"},
		{"битый JSON", `{"customer":`, 400, "invalid_json"},
		{"неверный тип", `{"items":"many"}`, 400, "invalid_type"},
		{"лишнее поле", `{"customer":{"name":"Иван","email":"a@b.c"},"hack":1}`, 400, "unknown_field"},
		{"слишком большое", `{"comment":"` + strings.Repeat("x", MaxBodyBytes) + `"}`, 413, "body_too_large"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := e.do("POST", "/api/v1/orders/validate", "", c.body)
			if w.Code != c.status || decodeError(t, w).Code != c.code {
				t.Fatalf("status %d, body %s", w.Code, w.Body)
			}
		})
	}
}

func TestLogin(t *testing.T) {
	e := newEnv(t)
	cases := []struct {
		body   map[string]string
		status int
	}{
		{map[string]string{"username": "alice", "password": "wrong-pass"}, 401},
		{map[string]string{"username": "nobody", "password": "alice-pass-1"}, 401},
		{map[string]string{"username": "a", "password": "alice-pass-1"}, 422},
		{map[string]string{"username": "alice", "password": "short"}, 422},
		{map[string]string{"username": "al ice", "password": "alice-pass-1"}, 422},
	}
	for _, c := range cases {
		if w := e.do("POST", "/auth/login", "", c.body); w.Code != c.status {
			t.Errorf("%v: %d, want %d", c.body, w.Code, c.status)
		}
	}
}

func TestLoginRateLimit(t *testing.T) {
	e := newEnv(t)
	wrong := map[string]string{"username": "alice", "password": "wrong-pass"}
	for i := range DefaultLoginMaxFailures {
		if w := e.do("POST", "/auth/login", "", wrong); w.Code != http.StatusUnauthorized {
			t.Fatalf("попытка %d: %d", i+1, w.Code)
		}
	}
	// После 5 неудач блокируется даже верный пароль — иначе перебор продолжался бы.
	w := e.do("POST", "/auth/login", "", map[string]string{"username": "alice", "password": "alice-pass-1"})
	// Окно отсчитывается от первой неудачи, а каждая проверка bcrypt занимает время
	// (под -race — около секунды), поэтому Retry-After чуть меньше 300 с, но не больше.
	retry, err := strconv.Atoi(w.Header().Get("Retry-After"))
	if w.Code != http.StatusTooManyRequests || err != nil || retry < 1 || retry > int(DefaultLoginLockout.Seconds()) {
		t.Fatalf("status %d, Retry-After %q", w.Code, w.Header().Get("Retry-After"))
	}
	if decodeError(t, w).Code != "too_many_attempts" {
		t.Fatalf("код ошибки: %s", w.Body)
	}
	// Блокировка не затрагивает других пользователей.
	e.login("bob", "bob-pass-12")
}

func TestForwardedForIgnored(t *testing.T) {
	e := newEnv(t)
	// Подмена X-Forwarded-For не даёт сбросить счётчик, выдав себя за другой IP.
	for i := range DefaultLoginMaxFailures + 1 {
		req := httptest.NewRequest("POST", "/auth/login", strings.NewReader(`{"username":"alice","password":"wrong-pass"}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-For", fmt.Sprintf("10.0.0.%d", i))
		w := httptest.NewRecorder()
		e.router.ServeHTTP(w, req)
		if i == DefaultLoginMaxFailures && w.Code != http.StatusTooManyRequests {
			t.Fatalf("попытка %d с подменённым IP: %d", i+1, w.Code)
		}
	}
}

func TestProtectedRoutes(t *testing.T) {
	e := newEnv(t)
	for _, p := range []string{"/api/v1/me", "/api/v1/orders"} {
		if w := e.do("GET", p, "", nil); w.Code != 401 {
			t.Errorf("%s без токена: %d", p, w.Code)
		}
	}
	tok := e.login("alice", "alice-pass-1")
	w := e.do("GET", "/api/v1/me", tok, nil)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"username":"alice"`) {
		t.Fatalf("me: %d %s", w.Code, w.Body)
	}
}

func TestServiceEndpoints(t *testing.T) {
	e := newEnv(t)
	if w := e.do("GET", "/ping", "", nil); w.Code != 200 || !strings.Contains(w.Body.String(), "pong") {
		t.Fatalf("ping: %s", w.Body)
	}
	w := e.do("GET", "/.well-known/jwks.json", "", nil)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"kty":"RSA"`) {
		t.Fatalf("jwks: %s", w.Body)
	}
	if w := e.do("GET", "/health/ready", "", nil); w.Code != 200 {
		t.Fatalf("ready: %d", w.Code)
	}
	e.ready.Store(false)
	if w := e.do("GET", "/health/ready", "", nil); w.Code != 503 {
		t.Fatalf("ready при остановке: %d", w.Code)
	}
	if w := e.do("GET", "/health/live", "", nil); w.Code != 200 {
		t.Fatalf("live при остановке: %d", w.Code)
	}
	if w := e.do("GET", "/nope", "", nil); w.Code != 404 {
		t.Fatalf("404: %d", w.Code)
	}
	if w := e.do("DELETE", "/ping", "", nil); w.Code != 405 {
		t.Fatalf("405: %d", w.Code)
	}
}

func TestRequestID(t *testing.T) {
	e := newEnv(t)
	req := httptest.NewRequest("GET", "/ping", nil)
	req.Header.Set("X-Request-ID", "abc-123")
	w := httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	if got := w.Header().Get("X-Request-ID"); got != "abc-123" {
		t.Fatalf("request id %q", got)
	}

	req = httptest.NewRequest("GET", "/ping", nil)
	req.Header.Set("X-Request-ID", "<script>")
	w = httptest.NewRecorder()
	e.router.ServeHTTP(w, req)
	if got := w.Header().Get("X-Request-ID"); got == "<script>" || len(got) != 36 {
		t.Fatalf("небезопасный id не заменён: %q", got)
	}
}
