// Задание 3 (Go): консольная программа, которую Python вызывает как подпроцесс.
//
// Протокол: на stdin приходит JSON {"numbers": [...]}, на stdout уходит JSON
// со статистикой. При ошибке в stdout пишется {"error": "..."} и код выхода 1,
// чтобы вызывающая сторона могла отличить сбой от нормального ответа.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
)

func main() {
	os.Exit(run(os.Stdin, os.Stdout))
}

func run(in io.Reader, out io.Writer) int {
	var req Request
	dec := json.NewDecoder(in)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(out, fmt.Errorf("некорректный JSON на входе: %w", err))
		return 1
	}

	stats, err := Compute(req.Numbers)
	if err != nil {
		writeError(out, err)
		return 1
	}

	if err := json.NewEncoder(out).Encode(stats); err != nil {
		fmt.Fprintln(os.Stderr, "ошибка записи ответа:", err)
		return 1
	}
	return 0
}

func writeError(out io.Writer, err error) {
	_ = json.NewEncoder(out).Encode(ErrorResponse{Error: err.Error()})
}
