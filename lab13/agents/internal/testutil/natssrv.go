// Пакет testutil — общие помощники тестов. Не используется в бинарниках агента и автомасштабировщика.
package testutil

import (
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
)

// StartNATS запускает встроенный сервер NATS с JetStream на случайном порту и каталоге
// t.TempDir(). Внешние процессы не нужны; сервер останавливается по завершении теста.
func StartNATS(t testing.TB) string {
	t.Helper()
	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      -1, // случайный свободный порт
		JetStream: true,
		StoreDir:  t.TempDir(),
		NoLog:     true,
		NoSigs:    true,
	}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("сервер NATS: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("NATS не поднялся за 10 с")
	}
	t.Cleanup(func() {
		srv.Shutdown()
		srv.WaitForShutdown()
	})
	return srv.ClientURL()
}
