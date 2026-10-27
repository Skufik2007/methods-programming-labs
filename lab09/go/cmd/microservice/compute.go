package main

import (
	"math"
	"math/rand"
	"runtime"
	"sync"
)

// CountPrimes считает простые числа в [2, limit] параллельным сегментированным
// решетом Эратосфена: диапазон делится на сегменты, которые разбирают workers горутин.
func CountPrimes(limit int, workers int) int {
	if limit < 2 {
		return 0
	}
	if workers <= 0 {
		workers = runtime.NumCPU()
	}

	// Базовые простые до sqrt(limit) — обычным решетом.
	// math.Sqrt считает во float64 и для больших limit может занизить корень на 1,
	// поэтому результат доводится до точного целого floor(sqrt(limit)).
	root := int(math.Sqrt(float64(limit)))
	for (root+1)*(root+1) <= limit {
		root++
	}
	base := simpleSieve(root)

	const segSize = 1 << 18
	// Буфер по числу воркеров: раздача сегментов не ждёт, пока воркер освободится.
	segments := make(chan int, workers)
	counts := make(chan int, workers)

	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			composite := make([]bool, segSize)
			total := 0
			for lo := range segments {
				hi := min(lo+segSize-1, limit)
				total += sieveSegment(lo, hi, base, composite[:hi-lo+1])
			}
			counts <- total
		}()
	}

	for lo := 2; lo <= limit; lo += segSize {
		segments <- lo
	}
	close(segments)
	wg.Wait()
	close(counts)

	total := 0
	for c := range counts {
		total += c
	}
	return total
}

func simpleSieve(n int) []int {
	composite := make([]bool, n+1)
	var primes []int
	for i := 2; i <= n; i++ {
		if composite[i] {
			continue
		}
		primes = append(primes, i)
		for j := i * i; j <= n; j += i {
			composite[j] = true
		}
	}
	return primes
}

// sieveSegment считает простые в [lo, hi]; buf длиной hi-lo+1 переиспользуется.
func sieveSegment(lo, hi int, base []int, buf []bool) int {
	clear(buf)
	for _, p := range base {
		if p*p > hi {
			break
		}
		start := max(p*p, (lo+p-1)/p*p)
		for j := start; j <= hi; j += p {
			buf[j-lo] = true
		}
	}
	count := 0
	for _, c := range buf {
		if !c {
			count++
		}
	}
	return count
}

// RandomMatrix строит детерминированную по seed матрицу size×size.
func RandomMatrix(size int, seed int64) [][]float64 {
	r := rand.New(rand.NewSource(seed))
	m := make([][]float64, size)
	for i := range m {
		m[i] = make([]float64, size)
		for j := range m[i] {
			m[i][j] = r.Float64()
		}
	}
	return m
}

// MatMul перемножает квадратные матрицы, распределяя строки результата по горутинам.
func MatMul(a, b [][]float64, workers int) [][]float64 {
	n := len(a)
	if workers <= 0 {
		workers = runtime.NumCPU()
	}

	// Транспонируем b, чтобы во внутреннем цикле читать память последовательно.
	bt := make([][]float64, n)
	for i := range bt {
		bt[i] = make([]float64, n)
		for j := range bt[i] {
			bt[i][j] = b[j][i]
		}
	}

	c := make([][]float64, n)
	rows := make(chan int)
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range rows {
				row := make([]float64, n)
				for j := 0; j < n; j++ {
					var s float64
					ai, bj := a[i], bt[j]
					for k := 0; k < n; k++ {
						s += ai[k] * bj[k]
					}
					row[j] = s
				}
				// Гонки нет: каждый индекс i приходит из канала ровно одному воркеру,
				// горутины пишут в разные элементы c, а чтение c происходит после wg.Wait().
				c[i] = row
			}
		}()
	}
	for i := 0; i < n; i++ {
		rows <- i
	}
	close(rows)
	wg.Wait()
	return c
}

// Trace — след матрицы, используется как компактная контрольная сумма результата.
func Trace(m [][]float64) float64 {
	var t float64
	for i := range m {
		t += m[i][i]
	}
	return t
}
