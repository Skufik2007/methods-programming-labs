package store

import (
	"context"
	"math"
	"testing"

	"github.com/alicebob/miniredis/v2"
)

// contract — сценарии, которые обязаны одинаково выполняться у всех реализаций Store.
func contract(t *testing.T, st Store) {
	t.Helper()
	ctx := context.Background()

	t.Run("SETNX создаёт один раз", func(t *testing.T) {
		ok, err := st.SetNX(ctx, "k-setnx", "v1")
		if err != nil || !ok {
			t.Fatalf("первый SetNX: ok=%v err=%v", ok, err)
		}
		ok, err = st.SetNX(ctx, "k-setnx", "v2")
		if err != nil || ok {
			t.Fatalf("повторный SetNX должен вернуть false: ok=%v err=%v", ok, err)
		}
	})

	t.Run("ZADD и ZRANGEBYSCORE по возрастанию score", func(t *testing.T) {
		key := "z-order"
		for _, p := range []struct {
			score  float64
			member string
		}{{30, "c"}, {10, "a"}, {20, "b"}, {20, "b-same"}} {
			if err := st.ZAdd(ctx, key, p.score, p.member); err != nil {
				t.Fatal(err)
			}
		}
		got, err := st.ZRangeByScore(ctx, key, math.Inf(-1), math.Inf(1), 0)
		if err != nil {
			t.Fatal(err)
		}
		want := []string{"a", "b", "b-same", "c"}
		if !equal(got, want) {
			t.Fatalf("порядок %v, ожидалось %v", got, want)
		}
	})

	t.Run("ZADD обновляет score существующего участника", func(t *testing.T) {
		key := "z-update"
		_ = st.ZAdd(ctx, key, 5, "m")
		_ = st.ZAdd(ctx, key, 50, "m")
		got, _ := st.ZRangeByScore(ctx, key, 0, 10, 0)
		if len(got) != 0 {
			t.Fatalf("после обновления score участник не должен попадать в [0,10]: %v", got)
		}
		got, _ = st.ZRangeByScore(ctx, key, 40, 60, 0)
		if !equal(got, []string{"m"}) {
			t.Fatalf("участник с новым score: %v", got)
		}
	})

	t.Run("ZRANGEBYSCORE границы включительно и limit", func(t *testing.T) {
		key := "z-bounds"
		for i := 1; i <= 5; i++ {
			_ = st.ZAdd(ctx, key, float64(i*10), string(rune('a'+i-1)))
		}
		got, err := st.ZRangeByScore(ctx, key, 20, 40, 0)
		if err != nil {
			t.Fatal(err)
		}
		if !equal(got, []string{"b", "c", "d"}) {
			t.Fatalf("[20,40] = %v", got)
		}
		got, _ = st.ZRangeByScore(ctx, key, math.Inf(-1), math.Inf(1), 2)
		if !equal(got, []string{"a", "b"}) {
			t.Fatalf("limit 2 = %v", got)
		}
		got, _ = st.ZRangeByScore(ctx, key, 1000, 2000, 0)
		if len(got) != 0 {
			t.Fatalf("пустой диапазон = %v", got)
		}
	})

	t.Run("ZREM возвращает true только при реальном удалении", func(t *testing.T) {
		key := "z-rem"
		_ = st.ZAdd(ctx, key, 1, "x")
		removed, err := st.ZRem(ctx, key, "x")
		if err != nil || !removed {
			t.Fatalf("первый ZRem: %v %v", removed, err)
		}
		removed, err = st.ZRem(ctx, key, "x")
		if err != nil || removed {
			t.Fatalf("повторный ZRem должен вернуть false: %v %v", removed, err)
		}
		removed, _ = st.ZRem(ctx, "no-such-key", "x")
		if removed {
			t.Fatal("ZRem по несуществующему ключу — false")
		}
	})

	t.Run("HSET, HGETALL, HINCRBY", func(t *testing.T) {
		key := "h-main"
		if err := st.HSet(ctx, key, map[string]string{"a": "1", "b": "two"}); err != nil {
			t.Fatal(err)
		}
		m, err := st.HGetAll(ctx, key)
		if err != nil || m["a"] != "1" || m["b"] != "two" || len(m) != 2 {
			t.Fatalf("HGetAll = %v err=%v", m, err)
		}
		n, err := st.HIncrBy(ctx, key, "counter", 2)
		if err != nil || n != 2 {
			t.Fatalf("HIncrBy 1 = %d %v", n, err)
		}
		n, _ = st.HIncrBy(ctx, key, "counter", 3)
		if n != 5 {
			t.Fatalf("HIncrBy 2 = %d, ожидалось 5", n)
		}
		n, _ = st.HIncrBy(ctx, key, "counter", -1)
		if n != 4 {
			t.Fatalf("HIncrBy отрицательный = %d, ожидалось 4", n)
		}
	})

	t.Run("HGETALL пустого ключа — пустая карта", func(t *testing.T) {
		m, err := st.HGetAll(ctx, "h-missing")
		if err != nil || m == nil || len(m) != 0 {
			t.Fatalf("HGetAll отсутствующего: %v (nil=%v) err=%v", m, m == nil, err)
		}
	})

	t.Run("DEL удаляет ключи любых типов", func(t *testing.T) {
		_, _ = st.SetNX(ctx, "d-str", "v")
		_ = st.ZAdd(ctx, "d-z", 1, "m")
		_ = st.HSet(ctx, "d-h", map[string]string{"f": "v"})
		if err := st.Del(ctx, "d-str", "d-z", "d-h"); err != nil {
			t.Fatal(err)
		}
		if ok, _ := st.SetNX(ctx, "d-str", "again"); !ok {
			t.Error("после DEL строковый ключ должен быть свободен для SetNX")
		}
		if got, _ := st.ZRangeByScore(ctx, "d-z", math.Inf(-1), math.Inf(1), 0); len(got) != 0 {
			t.Error("после DEL zset пуст")
		}
		if m, _ := st.HGetAll(ctx, "d-h"); len(m) != 0 {
			t.Error("после DEL hash пуст")
		}
	})

	t.Run("пустые аргументы не ошибка", func(t *testing.T) {
		if err := st.Del(ctx); err != nil {
			t.Errorf("Del без ключей: %v", err)
		}
		if err := st.HSet(ctx, "h-empty", nil); err != nil {
			t.Errorf("HSet без полей: %v", err)
		}
	})

	t.Run("Ping", func(t *testing.T) {
		if err := st.Ping(ctx); err != nil {
			t.Errorf("Ping: %v", err)
		}
	})
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestMemoryContract(t *testing.T) {
	t.Parallel()
	m := NewMemory()
	defer m.Close()
	contract(t, m)
}

func TestRedisContractMiniredis(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rs, err := NewRedis("redis://" + mr.Addr() + "/0")
	if err != nil {
		t.Fatalf("NewRedis: %v", err)
	}
	defer rs.Close()
	contract(t, rs)
}

// Redis: сценарии, специфичные для реализации на go-redis.
func TestRedisSpecific(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rs, err := NewRedis("redis://" + mr.Addr() + "/0")
	if err != nil {
		t.Fatal(err)
	}
	defer rs.Close()
	ctx := context.Background()

	t.Run("значение SETNX попадает в Redis", func(t *testing.T) {
		if _, err := rs.SetNX(ctx, "slot:x", "appt-1"); err != nil {
			t.Fatal(err)
		}
		if got, _ := mr.Get("slot:x"); got != "appt-1" {
			t.Fatalf("значение в Redis = %q", got)
		}
	})

	t.Run("ZRANGEBYSCORE с limit передаётся как COUNT", func(t *testing.T) {
		for i := 0; i < 5; i++ {
			_ = rs.ZAdd(ctx, "zc", float64(i), string(rune('a'+i)))
		}
		got, err := rs.ZRangeByScore(ctx, "zc", math.Inf(-1), math.Inf(1), 3)
		if err != nil || !equal(got, []string{"a", "b", "c"}) {
			t.Fatalf("limit 3: %v %v", got, err)
		}
	})

	t.Run("ошибка соединения возвращается и не паникует", func(t *testing.T) {
		mr2 := miniredis.RunT(t)
		broken, err := NewRedis("redis://" + mr2.Addr() + "/0")
		if err != nil {
			t.Fatal(err)
		}
		defer broken.Close()
		mr2.Close()
		if err := broken.Ping(ctx); err == nil {
			t.Error("Ping после остановки сервера должен вернуть ошибку")
		}
		if _, err := broken.SetNX(ctx, "k", "v"); err == nil {
			t.Error("SetNX без соединения должен вернуть ошибку")
		}
	})

	t.Run("неверный URL", func(t *testing.T) {
		if _, err := NewRedis("ht!tp://:::"); err == nil {
			t.Error("неверный REDIS_URL должен давать ошибку")
		}
	})
}

// Memory: тесты поведения, которое не входит в общий контракт.
func TestMemoryNoopLifecycle(t *testing.T) {
	t.Parallel()
	m := NewMemory()
	if err := m.Ping(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := m.Close(); err != nil {
		t.Fatal(err)
	}
}
