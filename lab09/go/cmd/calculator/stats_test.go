package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"
)

func TestCompute(t *testing.T) {
	got, err := Compute([]int64{1, 2, 3, 4, 5})
	if err != nil {
		t.Fatal(err)
	}
	want := Stats{Count: 5, Sum: 15, SumSquares: 55, Min: 1, Max: 5, Mean: 3}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestComputeErrors(t *testing.T) {
	cases := []struct {
		name    string
		numbers []int64
		want    error
	}{
		{"empty", nil, ErrEmpty},
		{"square overflow", []int64{math.MaxInt64}, ErrOverflow},
		{"sum of squares overflow", []int64{3_000_000_000, 3_000_000_000}, ErrOverflow},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, err := Compute(c.numbers); !errors.Is(err, c.want) {
				t.Fatalf("got %v, want %v", err, c.want)
			}
		})
	}
}

func TestRun(t *testing.T) {
	var out bytes.Buffer
	if code := run(strings.NewReader(`{"numbers":[1,2,3,4,5]}`), &out); code != 0 {
		t.Fatalf("exit code %d, output %s", code, out.String())
	}
	var s Stats
	if err := json.Unmarshal(out.Bytes(), &s); err != nil {
		t.Fatal(err)
	}
	if s.SumSquares != 55 {
		t.Fatalf("sum_squares = %d, want 55", s.SumSquares)
	}
}

func TestRunBadInput(t *testing.T) {
	for _, in := range []string{`not json`, `{"numbers":[]}`, `{"nums":[1]}`} {
		var out bytes.Buffer
		if code := run(strings.NewReader(in), &out); code != 1 {
			t.Errorf("input %q: exit code %d, want 1", in, code)
		}
		if !strings.Contains(out.String(), `"error"`) {
			t.Errorf("input %q: no error field in %s", in, out.String())
		}
	}
}
