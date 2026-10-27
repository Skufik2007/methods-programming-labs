package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	maxLineSize  = 1 << 20 // 1 МБ на одну строку запроса
	idleTimeout  = 5 * time.Minute
	writeTimeout = 10 * time.Second
)

// Request — запрос клиента.
type Request struct {
	Cmd     string  `json:"cmd"`
	Text    string  `json:"text,omitempty"`
	Numbers []int64 `json:"numbers,omitempty"`
}

// Response — ответ сервера. Ровно одно из полей Result/Error заполнено.
type Response struct {
	OK     bool   `json:"ok"`
	Result any    `json:"result,omitempty"`
	Error  string `json:"error,omitempty"`
}

// Server — TCP-сервер с учётом активных соединений для корректной остановки.
type Server struct {
	ln    net.Listener
	wg    sync.WaitGroup
	mu    sync.Mutex
	conns map[net.Conn]struct{}
}

func Listen(addr string) (*Server, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	return &Server{ln: ln, conns: make(map[net.Conn]struct{})}, nil
}

func (s *Server) Addr() net.Addr { return s.ln.Addr() }

// Serve принимает соединения, пока слушатель не будет закрыт.
func (s *Server) Serve() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				break
			}
			log.Printf("accept: %v", err)
			continue
		}
		s.track(conn, true)
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			defer s.track(conn, false)
			s.handle(conn)
		}()
	}
	s.wg.Wait()
}

// Close закрывает слушатель и все открытые соединения.
func (s *Server) Close() {
	s.ln.Close()
	s.mu.Lock()
	for c := range s.conns {
		c.Close()
	}
	s.mu.Unlock()
}

func (s *Server) track(c net.Conn, add bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if add {
		s.conns[c] = struct{}{}
	} else {
		delete(s.conns, c)
	}
}

func (s *Server) handle(conn net.Conn) {
	defer conn.Close()
	peer := conn.RemoteAddr()
	log.Printf("клиент подключился: %s", peer)
	defer log.Printf("клиент отключился: %s", peer)

	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 64*1024), maxLineSize)
	enc := json.NewEncoder(conn)

	for {
		if err := conn.SetReadDeadline(time.Now().Add(idleTimeout)); err != nil {
			log.Printf("%s: не удалось установить read deadline: %v", peer, err)
			return
		}
		if !scanner.Scan() {
			switch err := scanner.Err(); {
			case err == nil, errors.Is(err, net.ErrClosed):
				// клиент отключился сам или сервер останавливается
			case errors.Is(err, os.ErrDeadlineExceeded):
				log.Printf("%s: закрыто по тайм-ауту простоя (%s)", peer, idleTimeout)
			default:
				log.Printf("%s: ошибка чтения: %v", peer, err)
			}
			return
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}

		resp, quit := process(line)
		// Без write deadline медленный или зависший клиент, который не читает
		// ответы, мог бы навсегда заблокировать горутину на записи.
		if err := conn.SetWriteDeadline(time.Now().Add(writeTimeout)); err != nil {
			log.Printf("%s: не удалось установить write deadline: %v", peer, err)
			return
		}
		if err := enc.Encode(resp); err != nil {
			log.Printf("%s: ошибка записи: %v", peer, err)
			return
		}
		if quit {
			return
		}
	}
}

// process разбирает и выполняет одну команду. Второе значение — нужно ли закрыть соединение.
func process(line string) (Response, bool) {
	var req Request
	if err := json.Unmarshal([]byte(line), &req); err != nil {
		return fail(fmt.Errorf("некорректный JSON: %w", err)), false
	}

	switch req.Cmd {
	case "ping":
		return ok("pong"), false
	case "upper":
		return ok(strings.ToUpper(req.Text)), false
	case "reverse":
		r := []rune(req.Text)
		for i, j := 0, len(r)-1; i < j; i, j = i+1, j-1 {
			r[i], r[j] = r[j], r[i]
		}
		return ok(string(r)), false
	case "sum":
		var sum int64
		for _, n := range req.Numbers {
			next := sum + n
			if (n > 0 && next < sum) || (n < 0 && next > sum) {
				return fail(errors.New("переполнение int64")), false
			}
			sum = next
		}
		return ok(sum), false
	case "time":
		return ok(time.Now().UTC().Format(time.RFC3339)), false
	case "quit":
		return ok("bye"), true
	case "":
		return fail(errors.New("не указана команда (поле cmd)")), false
	default:
		return fail(fmt.Errorf("неизвестная команда %q", req.Cmd)), false
	}
}

func ok(v any) Response       { return Response{OK: true, Result: v} }
func fail(err error) Response { return Response{OK: false, Error: err.Error()} }
