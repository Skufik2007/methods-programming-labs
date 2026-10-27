package role

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/registry"
)

// rolesDir — каталог реальных ролей относительно пакета (agents/internal/role → ../../../roles).
const rolesDir = "../../../roles"

// Виды, которые обрабатывает Python (LLM-агенты). Go-агент их не принимает.
var llmKinds = []string{"intake", "feedback_reply"}

func goodRole(params string) string {
	return "# Роль: Тест\n\n## Роль\nописание\n\n## Параметры\n```yaml\n" + params + "\n```\n"
}

func TestParseRealRoles(t *testing.T) {
	t.Parallel()
	files, err := filepath.Glob(filepath.Join(rolesDir, "*.md"))
	if err != nil || len(files) == 0 {
		t.Fatalf("не найдены файлы ролей в %s: %v", rolesDir, err)
	}
	// ожидаемый kind для каждого файла — из контракта (раздел 8) и самих файлов ролей
	wantKind := map[string]string{
		"booking-a.md": "booking", "booking-b.md": "booking", "feedback.md": "feedback",
		"feedback_reply.md": "feedback_reply", "intake.md": "intake",
		"reminder.md": "reminder", "triage.md": "triage",
	}
	for _, f := range files {
		name := filepath.Base(f)
		want, known := wantKind[name]
		if !known {
			t.Errorf("новый файл роли %s: добавьте его в таблицу ожидаемых видов", name)
			continue
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			src, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			kinds := registry.Kinds()
			if contains(llmKinds, want) {
				// LLM-роли разбираются, только если их виды разрешены (Python-агенты)
				kinds = append(kinds, llmKinds...)
			}
			r, err := Parse(string(src), kinds)
			if err != nil {
				t.Fatalf("Parse: %v", err)
			}
			if r.Kind != want {
				t.Errorf("kind = %q, ожидалось %q", r.Kind, want)
			}
			if r.Step == "" || r.Title == "" {
				t.Errorf("step/title не заполнены: step=%q title=%q", r.Step, r.Title)
			}
			if r.Sections["Роль"] == "" {
				t.Errorf("раздел «Роль» пуст")
			}
		})
	}
}

func TestParseRealRolesFieldsAndGoKinds(t *testing.T) {
	t.Parallel()
	cases := []struct {
		file        string
		step        string
		concurrency int
	}{
		{"triage.md", "triage", 4},
		{"booking-a.md", "booking", 4},
		{"booking-b.md", "booking", 4},
		{"reminder.md", "reminder", 4},
		{"feedback.md", "feedback", 4},
		{"feedback_reply.md", "feedback_reply", 2},
	}
	for _, c := range cases {
		t.Run(c.file, func(t *testing.T) {
			t.Parallel()
			src, err := os.ReadFile(filepath.Join(rolesDir, c.file))
			if err != nil {
				t.Fatal(err)
			}
			r, err := Parse(string(src), append(registry.Kinds(), llmKinds...))
			if err != nil {
				t.Fatalf("Parse: %v", err)
			}
			if r.Step != c.step || r.Concurrency != c.concurrency {
				t.Errorf("step=%q concurrency=%d, ожидалось %q и %d", r.Step, r.Concurrency, c.step, c.concurrency)
			}
		})
	}
}

// Go-агент не принимает LLM-виды: intake и feedback_reply — только Python.
func TestLLMRolesRejectedByGoKinds(t *testing.T) {
	t.Parallel()
	src, err := os.ReadFile(filepath.Join(rolesDir, "intake.md"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = Parse(string(src), registry.Kinds())
	if err == nil || !strings.Contains(err.Error(), "неизвестный kind") {
		t.Fatalf("ожидалась ошибка неизвестного kind, получено %v", err)
	}
}

func TestParseErrors(t *testing.T) {
	t.Parallel()
	kinds := []string{"triage", "booking"}
	cases := []struct {
		name    string
		src     string
		wantErr string // подстрока ошибки
	}{
		{
			name:    "нет раздела Параметры",
			src:     "# Роль: X\n\n## Роль\nтекст\n",
			wantErr: "нет раздела «## Параметры»",
		},
		{
			name:    "в разделе нет блока yaml",
			src:     "# Роль: X\n## Параметры\nпросто текст без блока\n",
			wantErr: "нет блока ```yaml",
		},
		{
			name:    "битый YAML",
			src:     goodRole("kind: triage\n  step: [unclosed"),
			wantErr: "некорректный YAML",
		},
		{
			name:    "YAML — не отображение",
			src:     goodRole("- triage\n- booking"),
			wantErr: "должен быть отображением",
		},
		{
			name:    "пустой YAML-блок",
			src:     goodRole(""),
			wantErr: "должен быть отображением",
		},
		{
			name:    "нет kind",
			src:     goodRole("step: triage"),
			wantErr: "не указан kind",
		},
		{
			name:    "неизвестный kind",
			src:     goodRole("kind: dentist\nstep: triage"),
			wantErr: "неизвестный kind \"dentist\"",
		},
		{
			name:    "нет step",
			src:     goodRole("kind: triage"),
			wantErr: "не указан step",
		},
		{
			name:    "step с заглавными буквами",
			src:     goodRole("kind: triage\nstep: Triage"),
			wantErr: "step \"Triage\" недопустим",
		},
		{
			name:    "step с дефисом",
			src:     goodRole("kind: triage\nstep: feedback-reply"),
			wantErr: "недопустим",
		},
		{
			name:    "отрицательная concurrency",
			src:     goodRole("kind: triage\nstep: triage\nconcurrency: -1"),
			wantErr: "concurrency не может быть отрицательным",
		},
		{
			name:    "concurrency строкой",
			src:     goodRole("kind: triage\nstep: triage\nconcurrency: many"),
			wantErr: "некорректные поля kind/step/concurrency",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, err := Parse(c.src, kinds)
			if err == nil {
				t.Fatalf("ожидалась ошибка, содержащая %q", c.wantErr)
			}
			if !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("ошибка %q не содержит %q", err, c.wantErr)
			}
		})
	}
}

func TestParseDefaultsAndTitle(t *testing.T) {
	t.Parallel()
	src := "# Роль: Триаж\n\n## Роль\nА\n\n## Параметры\n```yaml\nkind: triage\nstep: triage\n```\n"
	r, err := Parse(src, []string{"triage"})
	if err != nil {
		t.Fatal(err)
	}
	if r.Title != "Триаж" {
		t.Errorf("Title = %q, ожидалось «Триаж»", r.Title)
	}
	if r.Concurrency != defaultConcurrency {
		t.Errorf("Concurrency = %d, ожидалось по умолчанию %d", r.Concurrency, defaultConcurrency)
	}
	if r.Sections["Роль"] != "А" {
		t.Errorf("раздел «Роль» = %q", r.Sections["Роль"])
	}
}

// Строки с «#» внутри YAML-блока не должны считаться заголовками разделов.
func TestYAMLCommentsAreNotHeaders(t *testing.T) {
	t.Parallel()
	src := "# Роль: X\n## Параметры\n```yaml\n# комментарий: не заголовок\n## это тоже не заголовок\nkind: triage\nstep: triage\n```\n"
	r, err := Parse(src, []string{"triage"})
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if _, bad := r.Sections["это тоже не заголовок"]; bad {
		t.Errorf("строка из кодового блока выделена как раздел")
	}
}

func TestCRLFLineEndings(t *testing.T) {
	t.Parallel()
	src := strings.ReplaceAll("# Роль: X\r\n## Роль\r\nтекст\r\n## Параметры\r\n```yaml\r\nkind: triage\r\nstep: triage\r\n```\r\n", "\r\n", "\n")
	src = strings.ReplaceAll(src, "\n", "\r\n") // файл из Windows
	r, err := Parse(src, []string{"triage"})
	if err != nil {
		t.Fatalf("CRLF: %v", err)
	}
	if r.Sections["Роль"] != "текст" {
		t.Errorf("раздел «Роль» = %q", r.Sections["Роль"])
	}
}

func TestParseFromFileOnDisk(t *testing.T) {
	t.Parallel()
	p := filepath.Join(t.TempDir(), "role.md")
	if err := os.WriteFile(p, []byte(goodRole("kind: reminder\nstep: reminder")), 0o644); err != nil {
		t.Fatal(err)
	}
	src, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Parse(string(src), []string{"reminder"}); err != nil {
		t.Fatal(err)
	}
}
