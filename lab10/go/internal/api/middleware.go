package api

import (
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"lab10/internal/auth"
)

const requestIDHeader = "X-Request-ID"

var safeRequestID = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// RequestID берёт X-Request-ID от клиента (если он безопасный) или генерирует новый
// и возвращает его в ответе — по нему можно связать логи разных сервисов.
func RequestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := c.GetHeader(requestIDHeader)
		if !safeRequestID.MatchString(id) {
			id = uuid.NewString()
		}
		c.Set(requestIDHeader, id)
		c.Header(requestIDHeader, id)
		c.Next()
	}
}

// Logger пишет одну структурированную запись на запрос.
func Logger(log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()

		attrs := []any{
			"method", c.Request.Method,
			"path", c.Request.URL.Path,
			"status", c.Writer.Status(),
			"duration_ms", float64(time.Since(start).Microseconds()) / 1000,
			"request_id", c.GetString(requestIDHeader),
			"client_ip", c.ClientIP(),
		}
		if cl, ok := auth.UserFromContext(c); ok {
			attrs = append(attrs, "user", cl.Subject)
		}
		level := slog.LevelInfo
		if c.Writer.Status() >= 500 {
			level = slog.LevelError
		}
		log.Log(c.Request.Context(), level, "request", attrs...)
	}
}

// Recovery превращает панику обработчика в 500 с JSON и записью в лог.
func Recovery(log *slog.Logger) gin.HandlerFunc {
	return gin.CustomRecoveryWithWriter(nil, func(c *gin.Context, err any) {
		log.Error("panic", "error", err, "request_id", c.GetString(requestIDHeader))
		fail(c, http.StatusInternalServerError, "internal", "внутренняя ошибка сервера")
	})
}

// LimitBody ограничивает размер тела: при превышении чтение вернёт *http.MaxBytesError.
func LimitBody(n int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, n)
		c.Next()
	}
}
