// Пакет logging настраивает slog: JSON в stdout и (если задан LOG_FILE) в файл.
package logging

import (
	"io"
	"log/slog"
	"os"
	"strings"
)

// New возвращает логгер и функцию закрытия файла.
func New(level, file, agentID string) (*slog.Logger, func(), error) {
	var lvl slog.Level
	if err := lvl.UnmarshalText([]byte(strings.ToUpper(level))); err != nil {
		lvl = slog.LevelInfo
	}
	var w io.Writer = os.Stdout
	closeFn := func() {}
	if file != "" {
		f, err := os.OpenFile(file, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			return nil, nil, err
		}
		w = io.MultiWriter(os.Stdout, f)
		closeFn = func() { _ = f.Close() }
	}
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{Level: lvl})
	return slog.New(h).With("agent_id", agentID), closeFn, nil
}
