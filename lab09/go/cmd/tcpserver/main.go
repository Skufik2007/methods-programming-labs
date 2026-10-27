// Задание 5 (Go): TCP-сервер, к которому подключается Python-клиент.
//
// Протокол построчный: клиент шлёт одну JSON-строку на запрос, сервер отвечает
// одной JSON-строкой. Каждое соединение обслуживается в отдельной горутине.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:9000", "адрес для прослушивания")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	srv, err := Listen(*addr)
	if err != nil {
		log.Fatalf("не удалось запустить сервер: %v", err)
	}
	log.Printf("TCP-сервер слушает %s", srv.Addr())

	go func() {
		<-ctx.Done()
		log.Println("остановка сервера...")
		srv.Close()
	}()

	srv.Serve()
	log.Println("сервер остановлен")
}
