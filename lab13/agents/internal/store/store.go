// Пакет store — минимальный интерфейс над Redis, достаточный агентам.
// Бизнес-логика зависит только от интерфейса, поэтому тестируется с Memory или miniredis.
package store

import "context"

type Store interface {
	// SetNX ставит значение, только если ключа нет; true — ключ создан нами.
	SetNX(ctx context.Context, key, value string) (bool, error)
	ZAdd(ctx context.Context, key string, score float64, member string) error
	// ZRangeByScore возвращает участников со score в [min, max] по возрастанию score;
	// limit <= 0 — без ограничения. Допустимы ±Inf.
	ZRangeByScore(ctx context.Context, key string, min, max float64, limit int64) ([]string, error)
	// ZRem возвращает true, если участник существовал и удалён именно этим вызовом.
	ZRem(ctx context.Context, key, member string) (bool, error)
	HSet(ctx context.Context, key string, values map[string]string) error
	HGetAll(ctx context.Context, key string) (map[string]string, error)
	HIncrBy(ctx context.Context, key, field string, n int64) (int64, error)
	Del(ctx context.Context, keys ...string) error
	Ping(ctx context.Context) error
	Close() error
}
