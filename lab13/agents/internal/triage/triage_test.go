package triage

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/role"
)

// loadRealParams читает реальную роль triage (roles/triage.md) — тесты работают с её фразами.
func loadRealParams(t *testing.T) Params {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "roles", "triage.md"))
	if err != nil {
		t.Fatal(err)
	}
	r, err := role.Parse(string(src), []string{"triage"})
	if err != nil {
		t.Fatalf("роль triage: %v", err)
	}
	var p Params
	if err := r.Params.Decode(&p); err != nil {
		t.Fatalf("параметры: %v", err)
	}
	return p
}

func realService(t *testing.T) *Service {
	t.Helper()
	svc, err := NewService(loadRealParams(t))
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func ptr(f float64) *float64 { return &f }

func TestRedFlagsFromComplaint(t *testing.T) {
	t.Parallel()
	svc := realService(t)
	cases := []struct {
		name      string
		complaint string
	}{
		{"боль в груди", "Сильная боль в груди, отдаёт в руку"},
		{"ё/е: отёк горла", "Отёк горла, трудно глотать"},
		{"ё/е: без ё в тексте", "Отек горла с утра"},
		{"верхний регистр", "ПОТЕРЯ СОЗНАНИЯ около часа назад"},
		{"пробелы внутри фразы", "не  могу   дышать вообще"},
		{"отравление", "Подозрение на отравление грибами"},
		{"суицидальные мысли", "Хочу умереть, всё надоело"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			out := svc.Assess(Input{Complaint: c.complaint})
			if out.Urgency != Emergency {
				t.Fatalf("urgency = %q, ожидалось emergency; reasons=%v", out.Urgency, out.Reasons)
			}
			if out.Specialty != "" || out.MaxWaitHours != 0 {
				t.Errorf("при emergency specialty=%q max_wait=%d, ожидалось пусто и 0", out.Specialty, out.MaxWaitHours)
			}
			if out.Recommendation != "Вызовите скорую: 103" {
				t.Errorf("рекомендация %q", out.Recommendation)
			}
			if len(out.Reasons) == 0 || !strings.Contains(out.Reasons[0], "красный флаг в жалобе") {
				t.Errorf("причины: %v", out.Reasons)
			}
		})
	}
}

func TestIntakeRedFlagsOnlyAdd(t *testing.T) {
	t.Parallel()
	svc := realService(t)

	// intake нашёл флаг, которого нет в тексте: эмергенси, даже если текст спокойный.
	out := svc.Assess(Input{Complaint: "Насморк третий день", RedFlags: []string{"судороги"}})
	if out.Urgency != Emergency {
		t.Fatalf("флаг от intake должен давать emergency, получено %q", out.Urgency)
	}
	if !strings.Contains(strings.Join(out.Reasons, ";"), "от intake") {
		t.Errorf("причина должна упоминать intake: %v", out.Reasons)
	}

	// пустые и пробельные флаги от intake игнорируются
	out = svc.Assess(Input{Complaint: "Насморк третий день", RedFlags: []string{"  ", ""}})
	if out.Urgency != Routine {
		t.Fatalf("пустые флаги не должны менять срочность: %q", out.Urgency)
	}

	// отсутствие флагов у intake не снимает флаг, найденный правилами по тексту
	out = svc.Assess(Input{Complaint: "Обморок в автобусе", RedFlags: nil})
	if out.Urgency != Emergency {
		t.Fatalf("флаг из текста нельзя снять отсутствием флагов от intake: %q", out.Urgency)
	}
}

func TestUrgentKeywords(t *testing.T) {
	t.Parallel()
	svc := realService(t)
	// Срочное ожидание задаёт роль (roles/triage.md, max_wait_hours.urgent), а не код.
	urgentWait := svc.p.MaxWaitHours[Urgent]
	cases := []struct {
		name     string
		in       Input
		wantSpec string
		wantRec  string
	}{
		{
			name:     "температура 39, специальность от intake",
			in:       Input{Complaint: "Температура 39.2 второй день", IntakeSpecialty: "therapist"},
			wantSpec: "therapist",
			wantRec:  fmt.Sprintf("Срочная запись к терапевту в течение %d ч", urgentWait),
		},
		{
			name:     "перелом: специальность от intake травматолог",
			in:       Input{Complaint: "Подозрение на перелом кисти", IntakeSpecialty: "traumatologist"},
			wantSpec: "traumatologist",
			wantRec:  fmt.Sprintf("Срочная запись к травматологу в течение %d ч", urgentWait),
		},
		{
			name:     "срочность: intake пустой, специальность по словарю",
			in:       Input{Complaint: "Ушиб головы, головная боль"},
			wantSpec: "neurologist",
			wantRec:  fmt.Sprintf("Срочная запись к неврологу в течение %d ч", urgentWait),
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			out := svc.Assess(c.in)
			if out.Urgency != Urgent {
				t.Fatalf("urgency = %q, ожидалось urgent; reasons=%v", out.Urgency, out.Reasons)
			}
			if out.MaxWaitHours != urgentWait {
				t.Errorf("max_wait_hours = %d, ожидалось %d (из роли)", out.MaxWaitHours, urgentWait)
			}
			if out.Specialty != c.wantSpec {
				t.Errorf("specialty = %q, ожидалось %q", out.Specialty, c.wantSpec)
			}
			if out.Recommendation != c.wantRec {
				t.Errorf("рекомендация %q, ожидалось %q", out.Recommendation, c.wantRec)
			}
		})
	}
}

func TestUrgentByKeywordCaseInsensitive(t *testing.T) {
	t.Parallel()
	out := realService(t).Assess(Input{Complaint: "ВЫСОКАЯ температура ночью"})
	if out.Urgency != Urgent {
		t.Fatalf("urgency = %q, ожидалось urgent", out.Urgency)
	}
}

func TestAgeRules(t *testing.T) {
	t.Parallel()
	svc := realService(t)
	cases := []struct {
		name string
		age  *float64
		want string
	}{
		{"нет возраста", nil, Routine},
		{"ребёнок 0.5 года", ptr(0.5), Urgent},
		{"ровно 0 (новорождённый)", ptr(0), Urgent},
		{"ровно 1 год — уже не младше года", ptr(1), Routine},
		{"34 года", ptr(34), Routine},
		{"74.9 года", ptr(74.9), Routine},
		{"ровно 75 лет", ptr(75), Urgent},
		{"90 лет", ptr(90), Urgent},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			out := svc.Assess(Input{Complaint: "Жалобы на слабость", Age: c.age})
			if out.Urgency != c.want {
				t.Fatalf("urgency = %q, ожидалось %q; reasons=%v", out.Urgency, c.want, out.Reasons)
			}
		})
	}
}

func TestAgeDoesNotDowngradeEmergency(t *testing.T) {
	t.Parallel()
	out := realService(t).Assess(Input{Complaint: "Обморок", Age: ptr(30)})
	if out.Urgency != Emergency {
		t.Fatalf("emergency не должен понижаться: %q", out.Urgency)
	}
}

func TestRoutineSpecialtyChoice(t *testing.T) {
	t.Parallel()
	svc := realService(t)
	cases := []struct {
		name string
		in   Input
		want string
	}{
		{
			name: "preferred при routine важнее intake",
			in:   Input{Complaint: "Болит сердце при нагрузке", PreferredSpecialty: "ophthalmologist", IntakeSpecialty: "cardiologist"},
			want: "ophthalmologist",
		},
		{
			name: "preferred при routine без intake",
			in:   Input{Complaint: "Сыпь на коже", PreferredSpecialty: "ent"},
			want: "ent",
		},
		{
			name: "preferred вне справочника игнорируется",
			in:   Input{Complaint: "Кашель неделю", PreferredSpecialty: "dentist", IntakeSpecialty: "neurologist"},
			want: "neurologist",
		},
		{
			name: "urgent: preferred игнорируется",
			in:   Input{Complaint: "Температура 39", PreferredSpecialty: "dermatologist", IntakeSpecialty: "therapist"},
			want: "therapist",
		},
		{
			name: "intake вернул специальность не из справочника → терапевт",
			in:   Input{Complaint: "Кашель неделю", IntakeSpecialty: "dentist"},
			want: "therapist",
		},
		{
			name: "intake валидная специальность",
			in:   Input{Complaint: "Головные боли", IntakeSpecialty: "neurologist"},
			want: "neurologist",
		},
		{
			name: "словарь: головная боль → невролог",
			in:   Input{Complaint: "Сильная головная боль по утрам"},
			want: "neurologist",
		},
		{
			name: "словарь: сыпь → дерматолог",
			in:   Input{Complaint: "Сыпь на руках"},
			want: "dermatologist",
		},
		{
			name: "словарь: горло → ЛОР",
			in:   Input{Complaint: "Болит горло"},
			want: "ent",
		},
		{
			name: "словарь: нет совпадений → терапевт по умолчанию",
			in:   Input{Complaint: "Общее недомогание"},
			want: "therapist",
		},
		{
			name: "словарь: больше совпадений побеждает",
			in:   Input{Complaint: "Давление и сердцебиение, аритмия"},
			want: "cardiologist",
		},
		{
			name: "словарь: ничья — порядок справочника (терапевт раньше офтальмолога)",
			in:   Input{Complaint: "Кашель и глаз красный"},
			want: "therapist",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			out := svc.Assess(c.in)
			if out.Specialty != c.want {
				t.Fatalf("specialty = %q, ожидалось %q (urgency=%s, reasons=%v)", out.Specialty, c.want, out.Urgency, out.Reasons)
			}
		})
	}
}

func TestRoutineDefaults(t *testing.T) {
	t.Parallel()
	out := realService(t).Assess(Input{Complaint: "Периодический осмотр"})
	if out.Urgency != Routine || out.MaxWaitHours != 72 {
		t.Fatalf("urgency=%q max_wait=%d, ожидалось routine/72", out.Urgency, out.MaxWaitHours)
	}
	if out.Recommendation != "Запись к терапевту" {
		t.Errorf("рекомендация %q", out.Recommendation)
	}
	if !strings.Contains(strings.Join(out.Reasons, ";"), "не найдено") {
		t.Errorf("причины: %v", out.Reasons)
	}
}

func TestMaxWaitHoursOverride(t *testing.T) {
	t.Parallel()
	p := loadRealParams(t)
	p.MaxWaitHours = map[string]int{"urgent": 2, "routine": 120}
	svc, err := NewService(p)
	if err != nil {
		t.Fatal(err)
	}
	if got := svc.Assess(Input{Complaint: "Высокая температура"}).MaxWaitHours; got != 2 {
		t.Errorf("urgent: %d, ожидалось 2", got)
	}
	if got := svc.Assess(Input{Complaint: "Насморк"}).MaxWaitHours; got != 120 {
		t.Errorf("routine: %d, ожидалось 120", got)
	}
	if got := svc.Assess(Input{Complaint: "Обморок"}).MaxWaitHours; got != 0 {
		t.Errorf("emergency: %d, ожидалось 0", got)
	}
}

func TestNewServiceErrors(t *testing.T) {
	t.Parallel()
	base := func() Params { return Params{EmergencyPhrases: []string{"обморок"}} }
	cases := []struct {
		name    string
		mutate  func(*Params)
		wantErr string
	}{
		{"пустой список красных флагов", func(p *Params) { p.EmergencyPhrases = nil }, "emergency_phrases пуст"},
		{"неизвестный уровень в max_wait_hours", func(p *Params) { p.MaxWaitHours = map[string]int{"later": 1} }, "неизвестный уровень"},
		{"отрицательное ожидание", func(p *Params) { p.MaxWaitHours = map[string]int{"urgent": -1} }, "не может быть отрицательным"},
		{"специальность из словаря не из справочника", func(p *Params) {
			p.SpecialtyPhrases = map[string]string{"зуб": "dentist"}
		}, "не специальность из справочника"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			p := base()
			c.mutate(&p)
			_, err := NewService(p)
			if err == nil || !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("ошибка %v, ожидалось %q", err, c.wantErr)
			}
		})
	}
}

func TestNewServiceDefaultsAge(t *testing.T) {
	t.Parallel()
	svc, err := NewService(Params{EmergencyPhrases: []string{"обморок"}})
	if err != nil {
		t.Fatal(err)
	}
	if svc.p.UrgentAgeBelow != 1 || svc.p.UrgentAgeFrom != 75 {
		t.Fatalf("возрастные границы по умолчанию: %v / %v", svc.p.UrgentAgeBelow, svc.p.UrgentAgeFrom)
	}
	if svc.p.MaxWaitHours[Routine] != 72 || svc.p.MaxWaitHours[Urgent] != 4 {
		t.Fatalf("max_wait по умолчанию: %v", svc.p.MaxWaitHours)
	}
}

func TestEmptyPhrasesAreIgnored(t *testing.T) {
	t.Parallel()
	svc, err := NewService(Params{EmergencyPhrases: []string{"обморок", "   "}, UrgentPhrases: []string{"", "жар"}})
	if err != nil {
		t.Fatal(err)
	}
	if out := svc.Assess(Input{Complaint: "Насморк"}); out.Urgency != Routine {
		t.Fatalf("пустая фраза не должна срабатывать как флаг: %q", out.Urgency)
	}
	if out := svc.Assess(Input{Complaint: "Сильный жар"}); out.Urgency != Urgent {
		t.Fatalf("фраза «жар» должна давать urgent: %q", out.Urgency)
	}
}

func TestClipLongFlag(t *testing.T) {
	t.Parallel()
	long := strings.Repeat("я", 150)
	out := realService(t).Assess(Input{Complaint: "Обычный день", RedFlags: []string{long}})
	reason := strings.Join(out.Reasons, ";")
	if !strings.Contains(reason, "…") || strings.Contains(reason, long) {
		t.Fatalf("длинный флаг должен быть обрезан: %q", reason)
	}
	if got := clip("короткий", 100); got != "короткий" {
		t.Errorf("clip не должен менять короткие строки: %q", got)
	}
}

func TestNormalize(t *testing.T) {
	t.Parallel()
	if got := normalize("  ОТЕК   Елка  "); got != "отек елка" {
		t.Fatalf("normalize = %q, ожидалось «отек елка»", got)
	}
}

// НАХОДКА: normalize делает strings.ToLower ДО замены «Ё»→«е», а вторая замена («Ё»→«е»)
// стоит после ToLower, когда заглавной «Ё» уже нет. В итоге «Ёлка» и «ОТЁК» дают «ёлка» и
// «отёк». Раздел 3 задания: ё/е и регистр не должны мешать. Тест падает до исправления.
func TestNormalizeCapitalYo(t *testing.T) {
	t.Parallel()
	if got := normalize("  ОТЁК   Ёлка  "); got != "отек елка" {
		t.Fatalf("normalize = %q, ожидалось «отек елка» (НАХОДКА: заглавная «Ё» не заменяется)", got)
	}
}

// НАХОДКА: та же ошибка в срабатывании красных флагов — текст заглавными буквами с «Ё».
func TestRedFlagUppercaseYo(t *testing.T) {
	t.Parallel()
	out := realService(t).Assess(Input{Complaint: "ОТЁК ГОРЛА, ГОЛОС ОСИПШИЙ"})
	if out.Urgency != Emergency {
		t.Fatalf("urgency = %q, ожидалось emergency (НАХОДКА: заглавная «Ё» не нормализуется)", out.Urgency)
	}
}

func TestHandlerPayload(t *testing.T) {
	t.Parallel()
	var params yaml.Node
	if err := yaml.Unmarshal([]byte("emergency_phrases: [обморок]\nurgent_phrases: [температура 39]\n"), &params); err != nil {
		t.Fatal(err)
	}
	h, err := New(*params.Content[0])
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	payload, _ := json.Marshal(map[string]any{
		"case":     map[string]any{"complaint": "Температура 39 и кашель", "age": 34, "preferred_specialty": ""},
		"previous": map[string]any{"specialty": "therapist", "red_flags": []string{}},
	})
	res, err := h.Handle(context.Background(), core.Task{Payload: payload})
	if err != nil {
		t.Fatalf("Handle: %v", err)
	}
	out, ok := res.(Output)
	if !ok || out.Urgency != Urgent {
		t.Fatalf("результат %#v", res)
	}

	// Некорректный JSON и пустая жалоба — постоянные ошибки (повтор не поможет).
	if _, err := h.Handle(context.Background(), core.Task{Payload: json.RawMessage(`{"case":`)}); err == nil || core.IsRetryable(err) {
		t.Errorf("битый JSON: err=%v retryable=%v", err, core.IsRetryable(err))
	}
	if _, err := h.Handle(context.Background(), core.Task{Payload: json.RawMessage(`{"case":{"complaint":"   "}}`)}); err == nil || core.IsRetryable(err) {
		t.Errorf("пустая жалоба: err=%v", err)
	}
}

func TestNewHandlerErrors(t *testing.T) {
	t.Parallel()
	var bad yaml.Node
	if err := yaml.Unmarshal([]byte("emergency_phrases: [обморок]\nmax_wait_hours: [1]\n"), &bad); err != nil {
		t.Fatal(err)
	}
	if _, err := New(*bad.Content[0]); err == nil {
		t.Error("неверный тип max_wait_hours должен давать ошибку")
	}
	var empty yaml.Node
	if err := yaml.Unmarshal([]byte("urgent_phrases: [жар]\n"), &empty); err != nil {
		t.Fatal(err)
	}
	if _, err := New(*empty.Content[0]); err == nil {
		t.Error("без emergency_phrases New должен падать")
	}
}
