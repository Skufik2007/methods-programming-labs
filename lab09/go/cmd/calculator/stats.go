package main

import (
	"errors"
	"math"
)

// Request — входные данные калькулятора.
type Request struct {
	Numbers []int64 `json:"numbers"`
}

// Stats — результат вычислений.
type Stats struct {
	Count      int     `json:"count"`
	Sum        int64   `json:"sum"`
	SumSquares int64   `json:"sum_squares"`
	Min        int64   `json:"min"`
	Max        int64   `json:"max"`
	Mean       float64 `json:"mean"`
}

// ErrorResponse — ответ при ошибке.
type ErrorResponse struct {
	Error string `json:"error"`
}

var (
	ErrEmpty    = errors.New("список чисел пуст")
	ErrOverflow = errors.New("переполнение int64 при вычислении суммы")
)

// Compute считает статистику по списку чисел с контролем переполнения.
func Compute(numbers []int64) (Stats, error) {
	if len(numbers) == 0 {
		return Stats{}, ErrEmpty
	}

	s := Stats{Count: len(numbers), Min: numbers[0], Max: numbers[0]}
	for _, n := range numbers {
		var ok bool
		if s.Sum, ok = addChecked(s.Sum, n); !ok {
			return Stats{}, ErrOverflow
		}
		sq, ok := mulChecked(n, n)
		if !ok {
			return Stats{}, ErrOverflow
		}
		if s.SumSquares, ok = addChecked(s.SumSquares, sq); !ok {
			return Stats{}, ErrOverflow
		}
		s.Min = min(s.Min, n)
		s.Max = max(s.Max, n)
	}
	s.Mean = float64(s.Sum) / float64(s.Count)
	return s, nil
}

func addChecked(a, b int64) (int64, bool) {
	c := a + b
	if (b > 0 && c < a) || (b < 0 && c > a) {
		return 0, false
	}
	return c, true
}

func mulChecked(a, b int64) (int64, bool) {
	if a == 0 || b == 0 {
		return 0, true
	}
	if a == math.MinInt64 || b == math.MinInt64 {
		return 0, false
	}
	c := a * b
	if c/b != a {
		return 0, false
	}
	return c, true
}
