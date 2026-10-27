package logging

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNewWritesJSONToFileWithAgentID(t *testing.T) {
	t.Parallel()
	file := filepath.Join(t.TempDir(), "agent.log")
	log, closeFn, err := New("debug", file, "triage-7")
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	log.Info("старт", "kind", "triage")
	log.Debug("детали")
	closeFn()

	data, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	if !strings.Contains(text, `"agent_id":"triage-7"`) || !strings.Contains(text, `"msg":"старт"`) {
		t.Fatalf("в файле нет ожидаемых полей: %s", text)
	}
	if !strings.Contains(text, `"msg":"детали"`) {
		t.Errorf("уровень debug должен писаться при LOG_LEVEL=debug")
	}
}

func TestNewRespectsLevelAndUnknownLevelDefaultsToInfo(t *testing.T) {
	t.Parallel()
	file := filepath.Join(t.TempDir(), "warn.log")
	log, closeFn, err := New("warn", file, "a")
	if err != nil {
		t.Fatal(err)
	}
	log.Info("не должно попасть")
	log.Warn("должно попасть")
	closeFn()
	data, _ := os.ReadFile(file)
	if strings.Contains(string(data), "не должно попасть") || !strings.Contains(string(data), "должно попасть") {
		t.Fatalf("фильтр уровня: %s", data)
	}

	// неизвестный уровень — info
	file2 := filepath.Join(t.TempDir(), "info.log")
	log2, closeFn2, err := New("чепуха", file2, "a")
	if err != nil {
		t.Fatal(err)
	}
	log2.Info("видно")
	log2.Debug("скрыто")
	closeFn2()
	data2, _ := os.ReadFile(file2)
	if !strings.Contains(string(data2), "видно") || strings.Contains(string(data2), "скрыто") {
		t.Fatalf("неизвестный уровень должен быть info: %s", data2)
	}
}

func TestNewWithoutFileUsesStdout(t *testing.T) {
	t.Parallel()
	log, closeFn, err := New("info", "", "a")
	if err != nil {
		t.Fatal(err)
	}
	defer closeFn()
	if log == nil {
		t.Fatal("логгер не создан")
	}
	log.Log(context.Background(), slog.LevelInfo, "в stdout")
}

func TestNewFailsOnUnwritableFile(t *testing.T) {
	t.Parallel()
	// Каталог вместо файла — открыть для записи нельзя.
	if _, _, err := New("info", t.TempDir(), "a"); err == nil {
		t.Fatal("ожидалась ошибка открытия файла")
	}
}
