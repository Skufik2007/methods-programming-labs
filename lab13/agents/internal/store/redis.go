package store

import (
	"context"
	"fmt"
	"strconv"

	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Redis — реализация поверх go-redis; каждая команда оборачивается в спан «redis <cmd>».
type Redis struct {
	c *redis.Client
}

// NewRedis разбирает URL вида redis://host:6379/0; соединение устанавливается лениво.
func NewRedis(url string) (*Redis, error) {
	opts, err := redis.ParseURL(url)
	if err != nil {
		return nil, fmt.Errorf("REDIS_URL: %w", err)
	}
	return &Redis{c: redis.NewClient(opts)}, nil
}

func (r *Redis) span(ctx context.Context, cmd, key string) (context.Context, func(error)) {
	ctx, sp := otel.Tracer("agents/redis").Start(ctx, "redis "+cmd, trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attribute.String("db.system", "redis"), attribute.String("db.redis.key", key)))
	return ctx, func(err error) {
		if err != nil {
			sp.RecordError(err)
			sp.SetStatus(codes.Error, err.Error())
		}
		sp.End()
	}
}

func (r *Redis) SetNX(ctx context.Context, key, value string) (bool, error) {
	ctx, end := r.span(ctx, "SET NX", key)
	ok, err := r.c.SetNX(ctx, key, value, 0).Result()
	end(err)
	return ok, err
}

func (r *Redis) ZAdd(ctx context.Context, key string, score float64, member string) error {
	ctx, end := r.span(ctx, "ZADD", key)
	err := r.c.ZAdd(ctx, key, redis.Z{Score: score, Member: member}).Err()
	end(err)
	return err
}

func (r *Redis) ZRangeByScore(ctx context.Context, key string, min, max float64, limit int64) ([]string, error) {
	ctx, end := r.span(ctx, "ZRANGEBYSCORE", key)
	opt := &redis.ZRangeBy{
		Min:   strconv.FormatFloat(min, 'f', -1, 64),
		Max:   strconv.FormatFloat(max, 'f', -1, 64),
		Count: max64(limit, 0),
	}
	res, err := r.c.ZRangeByScore(ctx, key, opt).Result()
	end(err)
	return res, err
}

func (r *Redis) ZRem(ctx context.Context, key, member string) (bool, error) {
	ctx, end := r.span(ctx, "ZREM", key)
	n, err := r.c.ZRem(ctx, key, member).Result()
	end(err)
	return n > 0, err
}

func (r *Redis) HSet(ctx context.Context, key string, values map[string]string) error {
	if len(values) == 0 {
		return nil
	}
	ctx, end := r.span(ctx, "HSET", key)
	args := make([]any, 0, len(values)*2)
	for k, v := range values {
		args = append(args, k, v)
	}
	err := r.c.HSet(ctx, key, args...).Err()
	end(err)
	return err
}

func (r *Redis) HGetAll(ctx context.Context, key string) (map[string]string, error) {
	ctx, end := r.span(ctx, "HGETALL", key)
	res, err := r.c.HGetAll(ctx, key).Result()
	end(err)
	return res, err
}

func (r *Redis) HIncrBy(ctx context.Context, key, field string, n int64) (int64, error) {
	ctx, end := r.span(ctx, "HINCRBY", key)
	res, err := r.c.HIncrBy(ctx, key, field, n).Result()
	end(err)
	return res, err
}

func (r *Redis) Del(ctx context.Context, keys ...string) error {
	if len(keys) == 0 {
		return nil
	}
	ctx, end := r.span(ctx, "DEL", keys[0])
	err := r.c.Del(ctx, keys...).Err()
	end(err)
	return err
}

func (r *Redis) Ping(ctx context.Context) error { return r.c.Ping(ctx).Err() }
func (r *Redis) Close() error                   { return r.c.Close() }

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
