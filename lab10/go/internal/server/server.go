// Package server запускает HTTP-сервер с корректной (graceful) остановкой.
package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"sync/atomic"
	"time"
)

// Options — параметры остановки.
type Options struct {
	// ShutdownTimeout — сколько ждать завершения активных запросов.
	ShutdownTimeout time.Duration
	// DrainDelay — пауза между переводом /health/ready в 503 и закрытием
	// слушателя: балансировщик (или docker/k8s) успевает перестать слать трафик.
	DrainDelay time.Duration
	// Ready сбрасывается в false в начале остановки.
	Ready  *atomic.Bool
	Logger *slog.Logger
}

// Run обслуживает запросы на ln, пока не будет отменён ctx (обычно по SIGINT/SIGTERM).
//
// Порядок остановки:
//  1. Ready = false — /health/ready начинает отвечать 503;
//  2. пауза DrainDelay;
//  3. http.Server.Shutdown: новые соединения не принимаются, простаивающие
//     keep-alive соединения закрываются, активные запросы дорабатывают;
//  4. если за ShutdownTimeout не успели — соединения закрываются принудительно
//     и возвращается ошибка.
func Run(ctx context.Context, srv *http.Server, ln net.Listener, opt Options) error {
	log := opt.Logger
	if log == nil {
		log = slog.Default()
	}
	if opt.Ready != nil {
		opt.Ready.Store(true)
	}

	serveErr := make(chan error, 1)
	go func() {
		log.Info("сервер запущен", "addr", ln.Addr().String())
		serveErr <- srv.Serve(ln)
	}()

	select {
	case err := <-serveErr:
		// Сервер упал сам, без сигнала остановки.
		return fmt.Errorf("serve: %w", err)
	case <-ctx.Done():
	}

	log.Info("получен сигнал остановки, начинаем graceful shutdown",
		"drain_delay", opt.DrainDelay.String(), "timeout", opt.ShutdownTimeout.String())
	if opt.Ready != nil {
		opt.Ready.Store(false)
	}
	if opt.DrainDelay > 0 {
		time.Sleep(opt.DrainDelay)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), opt.ShutdownTimeout)
	defer cancel()
	start := time.Now()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("активные запросы не завершились вовремя, закрываем принудительно", "error", err)
		_ = srv.Close()
		return fmt.Errorf("shutdown: %w", err)
	}
	if err := <-serveErr; err != nil && !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("serve: %w", err)
	}
	log.Info("сервер остановлен", "took", time.Since(start).Round(time.Millisecond).String())
	return nil
}
