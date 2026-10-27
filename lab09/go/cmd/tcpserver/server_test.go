package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"testing"
)

func TestProcess(t *testing.T) {
	cases := []struct {
		line   string
		ok     bool
		result any
		quit   bool
	}{
		{`{"cmd":"ping"}`, true, "pong", false},
		{`{"cmd":"upper","text":"привет"}`, true, "ПРИВЕТ", false},
		{`{"cmd":"reverse","text":"абв"}`, true, "вба", false},
		{`{"cmd":"sum","numbers":[1,2,3]}`, true, int64(6), false},
		{`{"cmd":"quit"}`, true, "bye", true},
		{`{"cmd":"nope"}`, false, nil, false},
		{`{}`, false, nil, false},
		{`garbage`, false, nil, false},
	}
	for _, c := range cases {
		resp, quit := process(c.line)
		if resp.OK != c.ok || quit != c.quit {
			t.Errorf("%s: got ok=%v quit=%v, want ok=%v quit=%v", c.line, resp.OK, quit, c.ok, c.quit)
		}
		if c.ok && resp.Result != c.result {
			t.Errorf("%s: result %v, want %v", c.line, resp.Result, c.result)
		}
	}
}

// Несколько клиентов одновременно — проверяет, что соединения обслуживаются параллельно.
func TestServerConcurrentClients(t *testing.T) {
	srv, err := Listen("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { srv.Serve(); close(done) }()
	defer func() { srv.Close(); <-done }()

	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			conn, err := net.Dial("tcp", srv.Addr().String())
			if err != nil {
				t.Error(err)
				return
			}
			defer conn.Close()

			fmt.Fprintf(conn, `{"cmd":"sum","numbers":[%d,%d]}`+"\n", i, i)
			line, err := bufio.NewReader(conn).ReadBytes('\n')
			if err != nil {
				t.Error(err)
				return
			}
			var resp struct {
				OK     bool  `json:"ok"`
				Result int64 `json:"result"`
			}
			if err := json.Unmarshal(line, &resp); err != nil {
				t.Error(err)
				return
			}
			if !resp.OK || resp.Result != int64(2*i) {
				t.Errorf("client %d: got %+v", i, resp)
			}
		}(i)
	}
	wg.Wait()
}
