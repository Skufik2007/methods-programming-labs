// Package api собирает HTTP-API на Gin: маршруты, middleware и обработчики.
package api

import (
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
	"github.com/go-playground/validator/v10"

	"lab10/internal/auth"
	"lab10/internal/orders"
)

// MaxBodyBytes ограничивает размер тела запроса.
const MaxBodyBytes = 1 << 20

// Ограничение подбора паролей по умолчанию: 5 неудач — блокировка на 5 минут.
const (
	DefaultLoginMaxFailures = 5
	DefaultLoginLockout     = 5 * time.Minute
)

// Deps — зависимости обработчиков.
type Deps struct {
	Issuer *auth.Issuer
	Users  *auth.Users
	Store  *orders.Store
	Logger *slog.Logger
	// Limiter ограничивает подбор паролей на /auth/login; nil — значения по умолчанию.
	Limiter *auth.LoginLimiter
	// Ready сбрасывается в false в начале graceful shutdown, чтобы /health/ready
	// сразу стал отдавать 503 и балансировщик перестал слать новые запросы.
	Ready *atomic.Bool
}

var setupValidator sync.Once

// configureBinding настраивает глобальный валидатор Gin один раз за процесс.
func configureBinding() error {
	var err error
	setupValidator.Do(func() {
		binding.EnableDecoderDisallowUnknownFields = true
		v, ok := binding.Validator.Engine().(*validator.Validate)
		if !ok {
			panic("binding.Validator не go-playground/validator")
		}
		err = orders.RegisterValidators(v)
	})
	return err
}

// NewRouter возвращает готовый к запуску gin.Engine.
func NewRouter(d Deps) (*gin.Engine, error) {
	if err := configureBinding(); err != nil {
		return nil, err
	}
	if d.Limiter == nil {
		d.Limiter = auth.NewLoginLimiter(DefaultLoginMaxFailures, DefaultLoginLockout)
	}
	h := &handlers{Deps: d}

	r := gin.New()
	r.HandleMethodNotAllowed = true
	// По умолчанию Gin доверяет X-Forwarded-For от любого клиента: подменив заголовок,
	// можно было бы выдать себя за другой IP и обойти ограничение попыток входа.
	// Прокси перед сервисом нет, поэтому IP клиента — адрес TCP-соединения.
	if err := r.SetTrustedProxies(nil); err != nil {
		return nil, err
	}
	r.Use(RequestID(), Logger(d.Logger), Recovery(d.Logger), LimitBody(MaxBodyBytes))

	r.GET("/ping", h.ping)
	r.GET("/health/live", h.live)
	r.GET("/health/ready", h.ready)
	r.GET("/.well-known/jwks.json", h.jwks)
	r.POST("/auth/login", h.login)
	r.POST("/api/v1/orders/validate", h.validateOrder)

	secured := r.Group("/api/v1", auth.Required(d.Issuer))
	secured.GET("/me", h.me)
	secured.POST("/orders", h.createOrder)
	secured.GET("/orders", h.listOrders)
	secured.GET("/orders/:id", h.getOrder)

	r.NoRoute(func(c *gin.Context) { fail(c, http.StatusNotFound, "not_found", "маршрут не найден") })
	r.NoMethod(func(c *gin.Context) {
		fail(c, http.StatusMethodNotAllowed, "method_not_allowed", "метод не поддерживается")
	})
	return r, nil
}
