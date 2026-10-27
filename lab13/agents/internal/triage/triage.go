// Пакет triage — чистая логика триажа: правила срочности и выбор специальности.
// Красные флаги определяются правилами здесь, а не моделью: intake может флаг добавить,
// но не снять (раздел 13 контракта).
package triage

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/directory"
)

const (
	Emergency = "emergency"
	Urgent    = "urgent"
	Routine   = "routine"
)

// Params — раздел параметров роли triage.
type Params struct {
	EmergencyPhrases []string          `yaml:"emergency_phrases"`
	UrgentPhrases    []string          `yaml:"urgent_phrases"`
	UrgentAgeBelow   float64           `yaml:"urgent_age_below"` // младше — не ниже urgent
	UrgentAgeFrom    float64           `yaml:"urgent_age_from"`  // от и старше — не ниже urgent
	MaxWaitHours     map[string]int    `yaml:"max_wait_hours"`
	SpecialtyPhrases map[string]string `yaml:"specialty_phrases"` // фраза -> специальность
}

type Input struct {
	Complaint          string
	Age                *float64
	PreferredSpecialty string
	IntakeSpecialty    string
	RedFlags           []string // previous.red_flags
}

type Output struct {
	Urgency        string   `json:"urgency"`
	Specialty      string   `json:"specialty"`
	MaxWaitHours   int      `json:"max_wait_hours"`
	Reasons        []string `json:"reasons"`
	Recommendation string   `json:"recommendation"`
}

type Service struct {
	p Params
	// нормализованные фразы с исходным написанием для причин
	emergency, urgent []phrase
	specKeys          []string // отсортированные нормализованные ключи словаря
}

type phrase struct{ norm, orig string }

func NewService(p Params) (*Service, error) {
	if p.UrgentAgeBelow == 0 {
		p.UrgentAgeBelow = 1
	}
	if p.UrgentAgeFrom == 0 {
		p.UrgentAgeFrom = 75
	}
	wait := map[string]int{Emergency: 0, Urgent: 4, Routine: 72}
	for k, v := range p.MaxWaitHours {
		if _, ok := wait[k]; !ok {
			return nil, fmt.Errorf("max_wait_hours: неизвестный уровень %q", k)
		}
		if v < 0 {
			return nil, fmt.Errorf("max_wait_hours.%s не может быть отрицательным", k)
		}
		wait[k] = v
	}
	p.MaxWaitHours = wait
	if len(p.EmergencyPhrases) == 0 {
		return nil, fmt.Errorf("emergency_phrases пуст: без красных флагов триаж небезопасен")
	}
	s := &Service{p: p}
	s.emergency = prepare(p.EmergencyPhrases)
	s.urgent = prepare(p.UrgentPhrases)
	norm := map[string]string{}
	for k, spec := range p.SpecialtyPhrases {
		if !directory.Valid(spec) {
			return nil, fmt.Errorf("specialty_phrases: %q — не специальность из справочника", spec)
		}
		norm[normalize(k)] = spec
		s.specKeys = append(s.specKeys, normalize(k))
	}
	sort.Strings(s.specKeys)
	s.p.SpecialtyPhrases = norm
	return s, nil
}

func prepare(list []string) []phrase {
	out := make([]phrase, 0, len(list))
	for _, o := range list {
		if n := normalize(o); n != "" {
			out = append(out, phrase{n, strings.TrimSpace(o)})
		}
	}
	return out
}

// normalize: нижний регистр, ё -> е, схлопывание пробелов.
func normalize(s string) string {
	// Сначала регистр, потом ё → е: иначе заглавная «Ё» после ToLower становится «ё»
	// и «ОТЁК ГОРЛА» не совпадает с красным флагом «отек горла».
	s = strings.ReplaceAll(strings.ToLower(s), "ё", "е")
	return strings.Join(strings.Fields(s), " ")
}

func matches(text string, list []phrase) []string {
	var out []string
	for _, p := range list {
		if strings.Contains(text, p.norm) {
			out = append(out, p.orig)
		}
	}
	return out
}

// Assess — вход -> выход без обращения к шине.
func (s *Service) Assess(in Input) Output {
	text := normalize(in.Complaint)
	var reasons []string

	urgency := Routine
	for _, m := range matches(text, s.emergency) {
		reasons = append(reasons, fmt.Sprintf("красный флаг в жалобе: «%s»", m))
		urgency = Emergency
	}
	for _, f := range in.RedFlags {
		if f = strings.TrimSpace(f); f != "" {
			reasons = append(reasons, fmt.Sprintf("красный флаг от intake: «%s»", clip(f, 100)))
			urgency = Emergency
		}
	}
	if urgency == Emergency {
		return Output{
			Urgency: Emergency, Specialty: "", MaxWaitHours: s.p.MaxWaitHours[Emergency],
			Reasons: reasons, Recommendation: "Вызовите скорую: 103",
		}
	}

	for _, m := range matches(text, s.urgent) {
		reasons = append(reasons, fmt.Sprintf("признак срочности: «%s»", m))
		urgency = Urgent
	}
	if in.Age != nil {
		age := *in.Age
		if age < s.p.UrgentAgeBelow || age >= s.p.UrgentAgeFrom {
			reasons = append(reasons, fmt.Sprintf("возраст %v: уровень не ниже urgent", age))
			urgency = Urgent
		}
	}
	if urgency == Routine {
		reasons = append(reasons, "красных флагов и признаков срочности не найдено")
	}

	specialty := s.pickSpecialty(in, text, urgency)
	rec := "Запись к " + directory.Dative(specialty)
	if urgency == Urgent {
		rec = fmt.Sprintf("Срочная запись к %s в течение %d ч", directory.Dative(specialty), s.p.MaxWaitHours[Urgent])
	}
	return Output{
		Urgency: urgency, Specialty: specialty, MaxWaitHours: s.p.MaxWaitHours[urgency],
		Reasons: reasons, Recommendation: rec,
	}
}

func (s *Service) pickSpecialty(in Input, text, urgency string) string {
	// Явный выбор пациента уважаем только при плановом обращении.
	if urgency == Routine && directory.Valid(in.PreferredSpecialty) {
		return in.PreferredSpecialty
	}
	switch {
	case directory.Valid(in.IntakeSpecialty):
		return in.IntakeSpecialty
	case in.IntakeSpecialty != "":
		return directory.Default // intake вернул не из справочника
	}
	hits := map[string]int{}
	for _, k := range s.specKeys {
		if strings.Contains(text, k) {
			hits[s.p.SpecialtyPhrases[k]]++
		}
	}
	best, bestN := directory.Default, 0
	for _, code := range directory.Codes() { // порядок справочника — детерминированный при равенстве
		if hits[code] > bestN {
			best, bestN = code, hits[code]
		}
	}
	return best
}

func clip(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// --- адаптер к задаче ---

type Handler struct{ svc *Service }

func New(params yaml.Node) (*Handler, error) {
	var p Params
	if err := params.Decode(&p); err != nil {
		return nil, fmt.Errorf("параметры triage: %w", err)
	}
	svc, err := NewService(p)
	if err != nil {
		return nil, fmt.Errorf("параметры triage: %w", err)
	}
	return &Handler{svc: svc}, nil
}

func (h *Handler) Handle(_ context.Context, t core.Task) (any, error) {
	var payload struct {
		Case struct {
			Complaint          string   `json:"complaint"`
			Age                *float64 `json:"age"`
			PreferredSpecialty string   `json:"preferred_specialty"`
		} `json:"case"`
		Previous struct {
			Specialty string   `json:"specialty"`
			RedFlags  []string `json:"red_flags"`
		} `json:"previous"`
	}
	if err := json.Unmarshal(t.Payload, &payload); err != nil {
		return nil, core.Permanent("некорректный payload: %v", err)
	}
	if strings.TrimSpace(payload.Case.Complaint) == "" {
		return nil, core.Permanent("в обращении нет жалобы (case.complaint)")
	}
	return h.svc.Assess(Input{
		Complaint:          payload.Case.Complaint,
		Age:                payload.Case.Age,
		PreferredSpecialty: payload.Case.PreferredSpecialty,
		IntakeSpecialty:    payload.Previous.Specialty,
		RedFlags:           payload.Previous.RedFlags,
	}), nil
}
