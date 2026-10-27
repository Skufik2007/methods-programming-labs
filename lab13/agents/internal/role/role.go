// Пакет role разбирает файл роли (раздел 8 контракта): разделы Markdown и YAML-блок
// после заголовка «## Параметры».
package role

import (
	"fmt"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	paramsSection      = "Параметры"
	defaultConcurrency = 4
)

var stepRe = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

type Role struct {
	Title       string            // из заголовка «# Роль: Триаж» -> «Триаж»
	Sections    map[string]string // имя раздела -> текст
	Kind        string
	Step        string
	Concurrency int
	Params      yaml.Node // весь YAML-блок; вид агента декодирует свои поля сам
}

type header struct {
	Kind        string `yaml:"kind"`
	Step        string `yaml:"step"`
	Concurrency int    `yaml:"concurrency"`
}

// Parse разбирает роль. kinds — допустимые виды агентов.
func Parse(src string, kinds []string) (*Role, error) {
	r := &Role{Sections: map[string]string{}}
	var (
		cur      string
		curLines []string
		inFence  bool
	)
	flush := func() {
		if cur != "" {
			r.Sections[cur] = strings.TrimSpace(strings.Join(curLines, "\n"))
		}
		curLines = nil
	}
	// Заголовки ищем только вне кодовых блоков: в YAML бывают строки с «#».
	for _, line := range strings.Split(strings.ReplaceAll(src, "\r\n", "\n"), "\n") {
		trim := strings.TrimSpace(line)
		if strings.HasPrefix(trim, "```") {
			inFence = !inFence
		}
		if !inFence && !strings.HasPrefix(trim, "```") {
			if strings.HasPrefix(line, "## ") {
				flush()
				cur = strings.TrimSpace(strings.TrimPrefix(line, "## "))
				continue
			}
			if strings.HasPrefix(line, "# ") && r.Title == "" {
				t := strings.TrimSpace(strings.TrimPrefix(line, "# "))
				t = strings.TrimSpace(strings.TrimPrefix(t, "Роль:"))
				r.Title = t
				continue
			}
		}
		curLines = append(curLines, line)
	}
	flush()

	body, ok := findParamsSection(src)
	if !ok {
		return nil, fmt.Errorf("в файле роли нет раздела «## %s»", paramsSection)
	}
	block, ok := firstYAMLBlock(body)
	if !ok {
		return nil, fmt.Errorf("в разделе «%s» нет блока ```yaml", paramsSection)
	}

	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(block), &doc); err != nil {
		return nil, fmt.Errorf("некорректный YAML в разделе «%s»: %w", paramsSection, err)
	}
	if len(doc.Content) == 0 || doc.Content[0].Kind != yaml.MappingNode {
		return nil, fmt.Errorf("YAML в разделе «%s» должен быть отображением (kind: ..., step: ...)", paramsSection)
	}
	r.Params = *doc.Content[0]
	var h header
	if err := r.Params.Decode(&h); err != nil {
		return nil, fmt.Errorf("некорректные поля kind/step/concurrency: %w", err)
	}
	if h.Kind == "" {
		return nil, fmt.Errorf("в параметрах роли не указан kind")
	}
	if !contains(kinds, h.Kind) {
		return nil, fmt.Errorf("неизвестный kind %q (допустимые: %s)", h.Kind, strings.Join(kinds, ", "))
	}
	if h.Step == "" {
		return nil, fmt.Errorf("в параметрах роли не указан step")
	}
	if !stepRe.MatchString(h.Step) {
		return nil, fmt.Errorf("step %q недопустим: нужны строчные латинские буквы, цифры и _", h.Step)
	}
	if h.Concurrency < 0 {
		return nil, fmt.Errorf("concurrency не может быть отрицательным")
	}
	if h.Concurrency == 0 {
		h.Concurrency = defaultConcurrency
	}
	r.Kind, r.Step, r.Concurrency = h.Kind, h.Step, h.Concurrency
	return r, nil
}

// findParamsSection возвращает сырой текст раздела «Параметры» (вместе с кодовыми блоками).
func findParamsSection(src string) (string, bool) {
	var (
		inFence, in bool
		out         []string
	)
	for _, line := range strings.Split(strings.ReplaceAll(src, "\r\n", "\n"), "\n") {
		trim := strings.TrimSpace(line)
		if !inFence && strings.HasPrefix(line, "## ") {
			if in {
				break
			}
			in = strings.TrimSpace(strings.TrimPrefix(line, "## ")) == paramsSection
			continue
		}
		if strings.HasPrefix(trim, "```") {
			inFence = !inFence
		}
		if in {
			out = append(out, line)
		}
	}
	return strings.Join(out, "\n"), in
}

func firstYAMLBlock(section string) (string, bool) {
	var (
		open bool
		buf  []string
	)
	for _, line := range strings.Split(section, "\n") {
		trim := strings.TrimSpace(line)
		switch {
		case !open && (trim == "```yaml" || trim == "```yml"):
			open = true
		case open && strings.HasPrefix(trim, "```"):
			return strings.Join(buf, "\n"), true
		case open:
			buf = append(buf, line)
		}
	}
	return "", false
}

func contains(list []string, v string) bool {
	for _, s := range list {
		if s == v {
			return true
		}
	}
	return false
}
