// Пакет feedback — опросы после приёма (schedule_survey) и разбор отзывов (analyze).
package feedback

import (
	"context"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/dispatch"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/reminder"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/stats"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

const (
	DueKey     = "mas:surveys:due"
	ItemPrefix = "mas:survey:"
	StatsKey   = "mas:feedback:stats"

	Positive = "positive"
	Neutral  = "neutral"
	Negative = "negative"
)

type Params struct {
	SurveyDelay   string              `yaml:"survey_delay"`
	Channel       string              `yaml:"channel"`
	SurveyText    string              `yaml:"survey_text"`
	PositiveWords []string            `yaml:"positive_words"`
	NegativeWords []string            `yaml:"negative_words"`
	Topics        map[string][]string `yaml:"topics"` // тема -> фразы
}

// Analysis — результат анализа отзыва.
type Analysis struct {
	Rating    int      `json:"rating"`
	Sentiment string   `json:"sentiment"`
	Escalate  bool     `json:"escalate"`
	Topics    []string `json:"topics"`
	NPSBucket string   `json:"nps_bucket"`
}

type Analyzer struct {
	pos, neg   []string
	topicNames []string
	topics     map[string][]string
}

func NewAnalyzer(p Params) *Analyzer {
	a := &Analyzer{pos: lowerAll(p.PositiveWords), neg: lowerAll(p.NegativeWords), topics: map[string][]string{}}
	for name, phrases := range p.Topics {
		a.topics[name] = lowerAll(phrases)
		a.topicNames = append(a.topicNames, name)
	}
	sort.Strings(a.topicNames)
	return a
}

func lowerAll(in []string) []string {
	out := make([]string, 0, len(in))
	for _, s := range in {
		if s = strings.ToLower(strings.TrimSpace(s)); s != "" {
			out = append(out, strings.ReplaceAll(s, "ё", "е"))
		}
	}
	return out
}

func count(text string, words []string) int {
	n := 0
	for _, w := range words {
		if strings.Contains(text, w) {
			n++
		}
	}
	return n
}

// Analyze: оценка 1..5, иначе ошибка. Тональность — по оценке; при нейтральной оценке
// 3 решает перевес слов из словарей. Отзыв 4–5 с негативными словами остаётся
// положительным: оценка пациента важнее эвристики.
func (a *Analyzer) Analyze(rating int, text string) (Analysis, error) {
	if rating < 1 || rating > 5 {
		return Analysis{}, fmt.Errorf("invalid rating: %d (ожидается 1..5)", rating)
	}
	t := strings.ReplaceAll(strings.ToLower(text), "ё", "е")
	res := Analysis{Rating: rating, Topics: []string{}}
	switch {
	case rating >= 4:
		res.Sentiment = Positive
	case rating <= 2:
		res.Sentiment = Negative
	default:
		switch n, p := count(t, a.neg), count(t, a.pos); {
		case n > p:
			res.Sentiment = Negative
		case p > n:
			res.Sentiment = Positive
		default:
			res.Sentiment = Neutral
		}
	}
	res.Escalate = res.Sentiment == Negative
	for _, name := range a.topicNames {
		if count(t, a.topics[name]) > 0 {
			res.Topics = append(res.Topics, name)
		}
	}
	switch rating {
	case 5:
		res.NPSBucket = "promoter"
	case 4:
		res.NPSBucket = "passive"
	default:
		res.NPSBucket = "detractor"
	}
	return res, nil
}

type Handler struct {
	an     *Analyzer
	p      Params
	delay  time.Duration
	disp   *dispatch.Dispatcher
	st     store.Store
	clock  core.Clock
	demo   time.Duration
	chanel string
}

func New(d core.Deps, params yaml.Node) (core.Instance, error) {
	var p Params
	if err := params.Decode(&p); err != nil {
		return core.Instance{}, fmt.Errorf("параметры feedback: %w", err)
	}
	if p.SurveyDelay == "" {
		p.SurveyDelay = "2h"
	}
	delay, err := reminder.ParseOffset(p.SurveyDelay)
	if err != nil {
		return core.Instance{}, fmt.Errorf("параметры feedback: survey_delay: %w", err)
	}
	if p.Channel == "" {
		p.Channel = "sms"
	}
	h := &Handler{an: NewAnalyzer(p), p: p, delay: delay, st: d.Store, clock: d.Clock,
		demo: d.SurveyDemoDelay, chanel: p.Channel}
	h.disp = dispatch.New(dispatch.Config{DueKey: DueKey, ItemPrefix: ItemPrefix, StatsKey: stats.Key("feedback")},
		d.Store, d.Clock, dispatch.NotifyDeliver(d.Sink, d.Clock, "survey"), d.Log)
	return core.Instance{Tasks: h, Background: []func(context.Context){h.disp.Run}}, nil
}

func (h *Handler) Handle(ctx context.Context, t core.Task) (any, error) {
	pl, err := core.ParsePayload(t.Payload)
	if err != nil {
		return nil, core.Permanent("некорректный payload: %v", err)
	}
	kind := t.Kind
	if kind == "" || kind == "feedback" {
		// Вид не указан: отзыв узнаём по наличию оценки.
		var r any
		if pl.Get("rating", &r, 0, 1) {
			kind = "analyze"
		} else {
			kind = "schedule_survey"
		}
	}
	switch kind {
	case "analyze":
		return h.analyze(ctx, pl)
	case "schedule_survey":
		return h.schedule(ctx, t, pl)
	}
	return nil, core.Permanent("неизвестный kind задачи feedback: %q", kind)
}

func (h *Handler) analyze(ctx context.Context, pl core.Payload) (any, error) {
	var ratingF float64
	if !pl.Get("rating", &ratingF, 0, 1) || ratingF != math.Trunc(ratingF) {
		return nil, core.Permanent("invalid rating: нужна целая оценка 1..5")
	}
	var text string
	pl.Get("text", &text, 0, 1)
	res, err := h.an.Analyze(int(ratingF), text)
	if err != nil {
		return nil, core.Permanent("%v", err)
	}
	h.recordStats(ctx, res)
	return res, nil
}

// recordStats ведёт mas:feedback:stats: счётчики по оценкам, NPS-корзинам и итоговый NPS.
func (h *Handler) recordStats(ctx context.Context, res Analysis) {
	fields := []string{"total", "rating_" + strconv.Itoa(res.Rating), res.NPSBucket + "s"}
	if res.Escalate {
		fields = append(fields, "escalated")
	}
	for _, f := range fields {
		if _, err := h.st.HIncrBy(ctx, StatsKey, f, 1); err != nil {
			return // статистика вторична: ответ пациенту важнее
		}
	}
	m, err := h.st.HGetAll(ctx, StatsKey)
	if err != nil {
		return
	}
	total, _ := strconv.Atoi(m["total"])
	prom, _ := strconv.Atoi(m["promoters"])
	detr, _ := strconv.Atoi(m["detractors"])
	if total > 0 {
		nps := math.Round(float64(prom-detr) / float64(total) * 100)
		_ = h.st.HSet(ctx, StatsKey, map[string]string{"nps": strconv.Itoa(int(nps))})
	}
}

func (h *Handler) schedule(ctx context.Context, t core.Task, pl core.Payload) (any, error) {
	var apptID, endsAtStr, patientID, patientName, phone string
	pl.Get("appointment_id", &apptID, 0, 2, 1)
	pl.Get("ends_at", &endsAtStr, 0, 2, 1)
	pl.Get("patient_id", &patientID, 1, 0)
	pl.Get("patient_name", &patientName, 1, 0)
	pl.Get("phone", &phone, 1, 0)

	now := h.clock.Now()
	endsAt, ok := h.resolveEnd(ctx, pl, endsAtStr)
	if !ok {
		endsAt = now // время конца приёма неизвестно — отсчитываем задержку от сейчас
	}
	sendAt := endsAt.Add(h.delay)
	if h.demo > 0 {
		sendAt = now.Add(h.demo)
	}
	id := uuid.NewString()
	if apptID != "" {
		id = core.StableID("survey", apptID)
	}
	text := h.p.SurveyText
	if text == "" {
		text = "Оцените, пожалуйста, приём по шкале от 1 до 5 и расскажите, что можно улучшить."
	}
	fields := map[string]string{
		"survey_id": id, "run_id": t.RunID, "appointment_id": apptID, "patient_id": patientID,
		"patient_name": patientName, "phone": phone, "channel": h.chanel, "text": text,
	}
	if err := h.disp.Schedule(ctx, id, sendAt, fields); err != nil {
		return nil, core.Transient("redis: %v", err)
	}
	return map[string]any{"survey_id": id, "send_at": sendAt.UTC().Format(time.RFC3339), "channel": h.chanel}, nil
}

// resolveEnd ищет время конца приёма: прямо в payload, иначе — в записи первого напоминания
// (в цепочке до feedback доходит только вывод reminder).
func (h *Handler) resolveEnd(ctx context.Context, pl core.Payload, direct string) (time.Time, bool) {
	if t, err := time.Parse(time.RFC3339, direct); err == nil {
		return t.UTC(), true
	}
	var rems []struct {
		ID string `json:"reminder_id"`
	}
	if !pl.Get("reminders", &rems, 2, 0) {
		return time.Time{}, false
	}
	for _, r := range rems {
		m, err := h.st.HGetAll(ctx, reminder.ItemPrefix+r.ID)
		if err != nil {
			continue
		}
		if t, err := time.Parse(time.RFC3339, m["ends_at"]); err == nil {
			return t.UTC(), true
		}
	}
	return time.Time{}, false
}
