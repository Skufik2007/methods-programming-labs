// Повышенное задание 1: микросервис на Go для тяжёлых вычислений,
// который вызывается из Python-приложения по HTTP.
//
// Эндпоинты:
//
//	GET  /health                       — проверка готовности
//	GET  /primes?limit=N[&workers=W]   — количество простых чисел до N
//	POST /matmul {"size":N,"seed":S}   — перемножение случайных матриц N×N
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "адрес HTTP-сервера")
	maxJobs := flag.Int("max-jobs", runtime.NumCPU(), "максимум одновременных тяжёлых вычислений")
	flag.Parse()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           logRequests(NewHandler(*maxJobs)),
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      2 * time.Minute,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("микросервис слушает http://%s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("ошибка сервера: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("остановка: ждём завершения текущих запросов...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.RequestURI(), time.Since(start).Round(time.Millisecond))
	})
}
