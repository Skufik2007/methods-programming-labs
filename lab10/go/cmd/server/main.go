// Go-сервис заказов на Gin.
//
// Конфигурация — переменные окружения (см. README, раздел «Конфигурация»).
// Остановка по SIGINT/SIGTERM — graceful: см. internal/server.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"lab10/internal/api"
	"lab10/internal/auth"
	"lab10/internal/orders"
	"lab10/internal/server"
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "проверить /health/ready запущенного сервиса и выйти (для Docker HEALTHCHECK)")
	flag.Parse()

	cfg, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, "конфигурация:", err)
		os.Exit(2)
	}
	if *healthcheck {
		os.Exit(probe(cfg.Addr))
	}

	log := newLogger(cfg.LogFormat, cfg.LogLevel)
	if err := run(cfg, log); err != nil {
		log.Error("сервис завершился с ошибкой", "error", err)
		os.Exit(1)
	}
}

func run(cfg config, log *slog.Logger) error {
	key, created, err := auth.LoadOrCreateKey(cfg.KeyFile)
	if err != nil {
		return err
	}
	switch {
	case cfg.KeyFile == "":
		log.Warn("JWT_PRIVATE_KEY_FILE не задан: сгенерирован временный ключ, токены не переживут перезапуск")
	case created:
		log.Info("создан новый ключ подписи JWT", "file", cfg.KeyFile)
	default:
		log.Info("ключ подписи JWT загружен", "file", cfg.KeyFile)
	}
	issuer, err := auth.NewIssuer(auth.Config{Issuer: cfg.Issuer, Audience: cfg.Audience, TTL: cfg.TokenTTL}, key)
	if err != nil {
		return err
	}
	users, err := auth.NewUsers(cfg.Users)
	if err != nil {
		return err
	}

	gin.SetMode(gin.ReleaseMode)
	ready := &atomic.Bool{}
	router, err := api.NewRouter(api.Deps{
		Issuer: issuer, Users: users, Store: orders.NewStore(), Logger: log, Ready: ready,
		Limiter: auth.NewLoginLimiter(cfg.LoginMaxFailures, cfg.LoginLockout),
	})
	if err != nil {
		return err
	}

	srv := &http.Server{
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
		ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	ln, err := net.Listen("tcp", cfg.Addr)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return server.Run(ctx, srv, ln, server.Options{
		ShutdownTimeout: cfg.ShutdownTimeout,
		DrainDelay:      cfg.DrainDelay,
		Ready:           ready,
		Logger:          log,
	})
}

type config struct {
	Addr            string
	LogFormat       string
	LogLevel        slog.Level
	Issuer          string
	Audience        string
	TokenTTL        time.Duration
	KeyFile         string
	Users           []auth.UserSpec
	ShutdownTimeout time.Duration
	DrainDelay      time.Duration

	LoginMaxFailures int
	LoginLockout     time.Duration
}

func loadConfig() (config, error) {
	c := config{
		Addr:      env("ADDR", ":8080"),
		LogFormat: env("LOG_FORMAT", "json"),
		Issuer:    env("JWT_ISSUER", "lab10-go-api"),
		Audience:  env("JWT_AUDIENCE", "lab10"),
		KeyFile:   os.Getenv("JWT_PRIVATE_KEY_FILE"),
	}
	var errs []error
	if err := c.LogLevel.UnmarshalText([]byte(env("LOG_LEVEL", "info"))); err != nil {
		errs = append(errs, errors.New("LOG_LEVEL: ожидается debug, info, warn или error"))
	}
	c.TokenTTL = duration("JWT_TTL", "15m", &errs)
	c.ShutdownTimeout = duration("SHUTDOWN_TIMEOUT", "20s", &errs)
	c.DrainDelay = duration("DRAIN_DELAY", "0s", &errs)
	c.LoginLockout = duration("LOGIN_LOCKOUT", api.DefaultLoginLockout.String(), &errs)
	maxFailures, err := strconv.Atoi(env("LOGIN_MAX_FAILURES", strconv.Itoa(api.DefaultLoginMaxFailures)))
	if err != nil || maxFailures < 1 {
		errs = append(errs, errors.New("LOGIN_MAX_FAILURES: ожидается целое число ≥ 1"))
	}
	c.LoginMaxFailures = maxFailures

	// Учётные записи: "имя:пароль:роль;имя2:пароль2:роль2".
	// Значение по умолчанию — демонстрационный пользователь для локального запуска.
	for _, spec := range strings.Split(env("DEMO_USERS", "student:student-pass-1:user"), ";") {
		parts := strings.SplitN(strings.TrimSpace(spec), ":", 3)
		if len(parts) != 3 || parts[0] == "" || len(parts[1]) < 8 {
			errs = append(errs, fmt.Errorf("DEMO_USERS: запись %q, нужен формат имя:пароль(8+ символов):роль", spec))
			continue
		}
		c.Users = append(c.Users, auth.UserSpec{Name: parts[0], Password: parts[1], Role: parts[2]})
	}
	return c, errors.Join(errs...)
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func duration(key, def string, errs *[]error) time.Duration {
	d, err := time.ParseDuration(env(key, def))
	if err != nil || d < 0 {
		*errs = append(*errs, fmt.Errorf("%s: ожидается длительность вида 15s/2m", key))
	}
	return d
}

func newLogger(format string, level slog.Level) *slog.Logger {
	opts := &slog.HandlerOptions{Level: level}
	var h slog.Handler = slog.NewJSONHandler(os.Stdout, opts)
	if format == "text" {
		h = slog.NewTextHandler(os.Stdout, opts)
	}
	return slog.New(h)
}

// probe запрашивает /health/ready у сервиса на этом же контейнере.
// В distroless-образе нет curl, поэтому healthcheck выполняет сам бинарь.
func probe(addr string) int {
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		return 1
	}
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://127.0.0.1:" + port + "/health/ready")
	if err != nil {
		return 1
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}
