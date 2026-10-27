// loadgen — генератор HTTP-нагрузки (аналог wrk/hey, которых нет под Windows).
//
// Держит c параллельных соединений в течение d и печатает JSON со статистикой:
// число запросов, RPS, ошибки и перцентили задержки. Используется bench/run_bench.py.
//
//	loadgen -url http://127.0.0.1:8080/ping -c 64 -d 10s
//	loadgen -url http://127.0.0.1:8080/api/v1/orders/validate -method POST -body order.json -c 64 -d 10s
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"slices"
	"sync"
	"time"
)

// errorBackoff — пауза воркера после ошибки соединения.
const errorBackoff = 10 * time.Millisecond

type Result struct {
	Requests  int     `json:"requests"`
	Errors    int     `json:"errors"`
	Non2xx    int     `json:"non_2xx"`
	DurationS float64 `json:"duration_s"`
	RPS       float64 `json:"rps"`
	Latency   Latency `json:"latency_ms"`
}

type Latency struct {
	Mean float64 `json:"mean"`
	P50  float64 `json:"p50"`
	P90  float64 `json:"p90"`
	P99  float64 `json:"p99"`
	Max  float64 `json:"max"`
}

func main() {
	url := flag.String("url", "", "адрес")
	method := flag.String("method", http.MethodGet, "HTTP-метод")
	bodyFile := flag.String("body", "", "файл с телом запроса (JSON)")
	conc := flag.Int("c", 16, "число параллельных соединений")
	dur := flag.Duration("d", 10*time.Second, "длительность замера")
	warmup := flag.Duration("warmup", time.Second, "прогрев перед замером (не учитывается)")
	flag.Parse()

	if *url == "" || *conc < 1 || *dur <= 0 {
		flag.Usage()
		os.Exit(2)
	}
	var body []byte
	if *bodyFile != "" {
		var err error
		if body, err = os.ReadFile(*bodyFile); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
	}

	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns:        *conc,
			MaxIdleConnsPerHost: *conc,
			DisableCompression:  true,
		},
	}
	if *warmup > 0 {
		run(client, *method, *url, body, *conc, *warmup)
	}
	res := run(client, *method, *url, body, *conc, *dur)
	_ = json.NewEncoder(os.Stdout).Encode(res)
}

// run держит conc воркеров, каждый шлёт запросы последовательно до истечения d.
func run(client *http.Client, method, url string, body []byte, conc int, d time.Duration) Result {
	type stats struct {
		lat            []time.Duration
		errors, non2xx int
	}
	all := make([]stats, conc)
	deadline := time.Now().Add(d)
	start := time.Now()

	var wg sync.WaitGroup
	for w := range conc {
		wg.Go(func() {
			s := &all[w]
			for time.Now().Before(deadline) {
				req, err := http.NewRequest(method, url, bytes.NewReader(body))
				if err != nil {
					s.errors++
					continue
				}
				if body != nil {
					req.Header.Set("Content-Type", "application/json")
				}
				t0 := time.Now()
				resp, err := client.Do(req)
				if err != nil {
					s.errors++
					// Сервер недоступен: без паузы цикл крутился бы вхолостую, занимая CPU,
					// который нужен самому тестируемому сервису на той же машине.
					time.Sleep(errorBackoff)
					continue
				}
				// Тело дочитывается, чтобы соединение вернулось в пул keep-alive.
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				s.lat = append(s.lat, time.Since(t0))
				if resp.StatusCode/100 != 2 {
					s.non2xx++
				}
			}
		})
	}
	wg.Wait()
	elapsed := time.Since(start)

	var lat []time.Duration
	res := Result{DurationS: elapsed.Seconds()}
	for _, s := range all {
		lat = append(lat, s.lat...)
		res.Errors += s.errors
		res.Non2xx += s.non2xx
	}
	res.Requests = len(lat)
	res.RPS = float64(res.Requests) / elapsed.Seconds()
	res.Latency = summarize(lat)
	return res
}

func summarize(lat []time.Duration) Latency {
	if len(lat) == 0 {
		return Latency{}
	}
	slices.Sort(lat)
	ms := func(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }
	// Перцентиль методом nearest-rank: наименьшее значение, не меньше которого p·n
	// замеров, — элемент с номером ceil(p·n) (с единицы). Для 1..100 p50 = 50, p99 = 99.
	pct := func(p float64) float64 {
		rank := int(math.Ceil(p * float64(len(lat))))
		return ms(lat[max(rank-1, 0)])
	}
	var sum time.Duration
	for _, l := range lat {
		sum += l
	}
	return Latency{
		Mean: ms(sum / time.Duration(len(lat))),
		P50:  pct(0.50),
		P90:  pct(0.90),
		P99:  pct(0.99),
		Max:  ms(lat[len(lat)-1]),
	}
}
