package feedback

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/core"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/reminder"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/role"
	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
)

// realParams — параметры реальной роли feedback (roles/feedback.md).
func realParams(t *testing.T) Params {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "roles", "feedback.md"))
	if err != nil {
		t.Fatal(err)
	}
	r, err := role.Parse(string(src), []string{"feedback"})
	if err != nil {
		t.Fatalf("роль feedback: %v", err)
	}
	var p Params
	if err := r.Params.Decode(&p); err != nil {
		t.Fatal(err)
	}
	return p
}

func realAnalyzer(t *testing.T) *Analyzer { return NewAnalyzer(realParams(t)) }

func TestAnalyzeRatingRange(t *testing.T) {
	t.Parallel()
	an := realAnalyzer(t)
	for _, bad := range []int{0, -1, 6, 100} {
		if _, err := an.Analyze(bad, "текст"); err == nil || !strings.Contains(err.Error(), "1..5") {
			t.Errorf("оценка %d должна отклоняться: %v", bad, err)
		}
	}
	for r := 1; r <= 5; r++ {
		if _, err := an.Analyze(r, ""); err != nil {
			t.Errorf("оценка %d допустима: %v", r, err)
		}
	}
}

func TestAnalyzeSentimentByRating(t *testing.T) {
	t.Parallel()
	an := realAnalyzer(t)
	cases := []struct {
		name     string
		rating   int
		text     string
		wantSent string
		wantEsc  bool
		wantNPS  string
	}{
		{"5 — позитив, сторонник", 5, "Спасибо, всё отлично", Positive, false, "promoter"},
		{"4 — позитив несмотря на негативное слово", 4, "Ждал долго, но врач вежлив", Positive, false, "passive"},
		{"4 без слов — позитив", 4, "", Positive, false, "passive"},
		{"2 — негатив, эскалация", 2, "Всё прошло нормально", Negative, true, "detractor"},
		{"1 — негатив", 1, "", Negative, true, "detractor"},
		{"3 с перевесом позитива", 3, "спасибо, быстро и вежливо, но очередь", Positive, false, "detractor"},
		{"3 с перевесом негатива", 3, "ужасно, грубо, долго", Negative, true, "detractor"},
		{"3 без слов — нейтрально", 3, "не знаю", Neutral, false, "detractor"},
		{"3 при равенстве — нейтрально", 3, "спасибо, но очередь", Neutral, false, "detractor"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			res, err := an.Analyze(c.rating, c.text)
			if err != nil {
				t.Fatal(err)
			}
			if res.Sentiment != c.wantSent {
				t.Errorf("sentiment = %q, ожидалось %q", res.Sentiment, c.wantSent)
			}
			if res.Escalate != c.wantEsc {
				t.Errorf("escalate = %v, ожидалось %v", res.Escalate, c.wantEsc)
			}
			if res.NPSBucket != c.wantNPS {
				t.Errorf("nps_bucket = %q, ожидалось %q", res.NPSBucket, c.wantNPS)
			}
			if res.Rating != c.rating {
				t.Errorf("rating = %d", res.Rating)
			}
		})
	}
}

func TestNPSBuckets(t *testing.T) {
	t.Parallel()
	an := realAnalyzer(t)
	want := map[int]string{5: "promoter", 4: "passive", 3: "detractor", 2: "detractor", 1: "detractor"}
	for r, b := range want {
		res, _ := an.Analyze(r, "")
		if res.NPSBucket != b {
			t.Errorf("оценка %d: %q, ожидалось %q", r, res.NPSBucket, b)
		}
	}
}

func TestAnalyzeTopics(t *testing.T) {
	t.Parallel()
	an := realAnalyzer(t)
	res, _ := an.Analyze(2, "Долго ждал в очереди, врач опоздал, цена высокая")
	got := strings.Join(res.Topics, ",")
	// порядок тем — по имени (сортировка строк): врач < ожидание < стоимость
	if got != "врач,ожидание,стоимость" {
		t.Fatalf("topics = %q", got)
	}
	none, _ := an.Analyze(5, "Всё замечательно")
	if none.Topics == nil || len(none.Topics) != 0 {
		t.Fatalf("без тем — пустой (не nil) список: %#v", none.Topics)
	}
}

func TestTopicsAreSortedAndYoNormalized(t *testing.T) {
	t.Parallel()
	an := NewAnalyzer(Params{
		Topics: map[string][]string{"цена": {"дешёвый"}, "запись": {"талон"}},
	})
	if strings.Join(an.topicNames, ",") != "запись,цена" {
		t.Fatalf("порядок тем: %v", an.topicNames)
	}
	// «дешёвый» в фразе и «дешевый» в тексте совпадают: ё/е нормализуются у обоих.
	res, _ := an.Analyze(3, "Очень Дешевый вариант")
	if strings.Join(res.Topics, ",") != "цена" {
		t.Fatalf("topics = %v", res.Topics)
	}
	res, _ = an.Analyze(3, "Очень дешёвый вариант")
	if strings.Join(res.Topics, ",") != "цена" {
		t.Fatalf("ё в тексте: topics = %v", res.Topics)
	}
}

func TestEmptyWordsIgnored(t *testing.T) {
	t.Parallel()
	an := NewAnalyzer(Params{PositiveWords: []string{"  ", ""}, NegativeWords: []string{"плохо"}})
	res, _ := an.Analyze(3, "всё плохо")
	if res.Sentiment != Negative {
		t.Fatalf("пустые слова не должны влиять: %q", res.Sentiment)
	}
}

func TestHandleKindDetectionAndErrors(t *testing.T) {
	t.Parallel()
	h := newHandler(t, store.NewMemory(), time.Now)
	ctx := context.Background()

	// kind analyze по наличию оценки, даже если kind пустой
	res, err := h.Handle(ctx, core.Task{Payload: payload(t, map[string]any{"rating": 5, "text": "спасибо"})})
	if err != nil {
		t.Fatal(err)
	}
	if res.(Analysis).Sentiment != Positive {
		t.Fatalf("результат: %#v", res)
	}

	// kind schedule_survey, если оценки нет
	if _, err := h.Handle(ctx, core.Task{Kind: "feedback", Payload: payload(t, map[string]any{"appointment_id": "a"})}); err != nil {
		t.Fatalf("без оценки и kind=feedback — опрос: %v", err)
	}

	// неизвестный kind
	if _, err := h.Handle(ctx, core.Task{Kind: "exotic", Payload: payload(t, map[string]any{})}); err == nil || core.IsRetryable(err) {
		t.Errorf("неизвестный kind: %v", err)
	}

	// битый JSON
	if _, err := h.Handle(ctx, core.Task{Kind: "analyze", Payload: json.RawMessage(`[`)}); err == nil || core.IsRetryable(err) {
		t.Errorf("битый JSON: %v", err)
	}
}

func TestAnalyzeTaskValidation(t *testing.T) {
	t.Parallel()
	h := newHandler(t, store.NewMemory(), time.Now)
	ctx := context.Background()
	cases := []struct {
		name    string
		payload map[string]any
	}{
		{"дробная оценка", map[string]any{"rating": 2.5}},
		{"оценка 0", map[string]any{"rating": 0}},
		{"оценка 6", map[string]any{"rating": 6}},
		{"оценка строкой", map[string]any{"rating": "5"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, err := h.Handle(ctx, core.Task{Kind: "analyze", Payload: payload(t, c.payload)})
			if err == nil {
				t.Fatal("ожидалась ошибка")
			}
			if core.IsRetryable(err) {
				t.Errorf("ошибка оценки не повторяется (раздел 4.2): %v", err)
			}
		})
	}
}

func TestStatsAndNPS(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	h := newHandler(t, st, time.Now)
	ctx := context.Background()
	for _, r := range []int{5, 5, 1} { // 2 сторонника, 1 критик
		if _, err := h.Handle(ctx, core.Task{Kind: "analyze", Payload: payload(t, map[string]any{"rating": r})}); err != nil {
			t.Fatal(err)
		}
	}
	m, _ := st.HGetAll(ctx, StatsKey)
	if m["total"] != "3" || m["rating_5"] != "2" || m["rating_1"] != "1" {
		t.Errorf("счётчики: %v", m)
	}
	if m["promoters"] != "2" || m["detractors"] != "1" || m["escalated"] != "1" {
		t.Errorf("корзины/эскалация: %v", m)
	}
	// NPS = round((2 - 1) / 3 · 100) = 33
	if m["nps"] != "33" {
		t.Errorf("nps = %q, ожидалось 33", m["nps"])
	}
}

func TestStatsFailureDoesNotFailAnalysis(t *testing.T) {
	t.Parallel()
	h := newHandler(t, &failHIncr{Store: store.NewMemory()}, time.Now)
	res, err := h.Handle(context.Background(), core.Task{Kind: "analyze", Payload: payload(t, map[string]any{"rating": 4})})
	if err != nil || res.(Analysis).Rating != 4 {
		t.Fatalf("статистика вторична: err=%v res=%#v", err, res)
	}
}

type failHIncr struct{ store.Store }

func (f *failHIncr) HIncrBy(context.Context, string, string, int64) (int64, error) {
	return 0, errors.New("сеть")
}

func TestScheduleSurveyTimingAndStableID(t *testing.T) {
	t.Parallel()
	st := store.NewMemory()
	now := time.Date(2026, 10, 15, 8, 0, 0, 0, time.UTC)
	h := newHandler(t, st, func() time.Time { return now })
	ctx := context.Background()

	p := payload(t, map[string]any{
		"appointment_id": "appt-42", "ends_at": "2026-10-15T09:20:00Z",
		"patient_id": "p-42", "patient_name": "Иванова", "phone": "+7 900",
	})
	res, err := h.Handle(ctx, core.Task{Kind: "schedule_survey", RunID: "r-42", Payload: p})
	if err != nil {
		t.Fatal(err)
	}
	out := res.(map[string]any)
	// опрос — через survey_delay (2h) после конца приёма
	if out["send_at"] != "2026-10-15T11:20:00Z" {
		t.Fatalf("send_at = %v, ожидалось 11:20Z", out["send_at"])
	}
	if out["survey_id"] != core.StableID("survey", "appt-42") {
		t.Errorf("survey_id должен быть детерминированным по appointment_id")
	}
	// повтор задачи (таймаут оркестратора) — опрос не дублируется
	if _, err := h.Handle(ctx, core.Task{Kind: "schedule_survey", RunID: "r-42", Payload: p}); err != nil {
		t.Fatal(err)
	}
	if n := countDue(t, st); n != 1 {
		t.Fatalf("опросов в очереди %d, ожидался 1", n)
	}
}

func countDue(t *testing.T, st store.Store) int {
	t.Helper()
	ids, err := st.ZRangeByScore(context.Background(), DueKey, -1e18, 1e18, 0)
	if err != nil {
		t.Fatal(err)
	}
	return len(ids)
}

func TestScheduleSurveyFallbacks(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 15, 8, 0, 0, 0, time.UTC)

	t.Run("нет времени конца — отсчёт от now", func(t *testing.T) {
		t.Parallel()
		h := newHandler(t, store.NewMemory(), func() time.Time { return now })
		res, err := h.Handle(context.Background(), core.Task{Kind: "schedule_survey", RunID: "r",
			Payload: payload(t, map[string]any{"patient_id": "p"})})
		if err != nil {
			t.Fatal(err)
		}
		if got := res.(map[string]any)["send_at"]; got != "2026-10-15T10:00:00Z" {
			t.Errorf("send_at = %v, ожидалось now+2h", got)
		}
	})

	t.Run("конец приёма из первого напоминания (previous.reminders)", func(t *testing.T) {
		t.Parallel()
		st := store.NewMemory()
		_ = st.HSet(context.Background(), reminder.ItemPrefix+"rem-1",
			map[string]string{"ends_at": "2026-10-15T09:40:00Z"})
		h := newHandler(t, st, func() time.Time { return now })
		res, err := h.Handle(context.Background(), core.Task{Kind: "schedule_survey", RunID: "r",
			Payload: payload(t, map[string]any{"previous": map[string]any{
				"appointment_id": "appt-r",
				"reminders":      []map[string]any{{"reminder_id": "rem-1"}},
			}})})
		if err != nil {
			t.Fatal(err)
		}
		if got := res.(map[string]any)["send_at"]; got != "2026-10-15T11:40:00Z" {
			t.Errorf("send_at = %v, ожидалось 11:40Z (конец 09:40 + 2h)", got)
		}
	})

	t.Run("демо-задержка переопределяет время", func(t *testing.T) {
		t.Parallel()
		h := newHandler(t, store.NewMemory(), func() time.Time { return now })
		h.demo = 15 * time.Second
		res, err := h.Handle(context.Background(), core.Task{Kind: "schedule_survey", RunID: "r",
			Payload: payload(t, map[string]any{"ends_at": "2026-10-15T09:00:00Z"})})
		if err != nil {
			t.Fatal(err)
		}
		if got := res.(map[string]any)["send_at"]; got != "2026-10-15T08:00:15Z" {
			t.Errorf("send_at = %v, ожидалось now+15s", got)
		}
	})

	t.Run("сбой Redis при постановке — повторяемо", func(t *testing.T) {
		t.Parallel()
		h := newHandler(t, &failSetNX{Store: store.NewMemory()}, func() time.Time { return now })
		_, err := h.Handle(context.Background(), core.Task{Kind: "schedule_survey", RunID: "r",
			Payload: payload(t, map[string]any{"appointment_id": "a"})})
		if err == nil || !core.IsRetryable(err) {
			t.Fatalf("ожидалась повторяемая ошибка: %v", err)
		}
	})
}

type failSetNX struct{ store.Store }

func (f *failSetNX) SetNX(context.Context, string, string) (bool, error) {
	return false, errors.New("сеть")
}

func TestNewParams(t *testing.T) {
	t.Parallel()
	mk := func(src string) (core.Instance, error) {
		var params yaml.Node
		if err := yaml.Unmarshal([]byte(src), &params); err != nil {
			t.Fatal(err)
		}
		loc, _ := time.LoadLocation("Europe/Moscow")
		return New(core.Deps{AgentID: "f", Step: "feedback", Store: store.NewMemory(), Loc: loc,
			Clock: time.Now, Sink: nil, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}, *params.Content[0])
	}
	inst, err := mk("positive_words: [спасибо]\n")
	if err != nil {
		t.Fatalf("по умолчанию: %v", err)
	}
	if len(inst.Background) != 1 {
		t.Errorf("фоновый диспетчер не создан")
	}
	if _, err := mk("survey_delay: вечно\n"); err == nil || !strings.Contains(err.Error(), "survey_delay") {
		t.Errorf("неверный survey_delay: %v", err)
	}
	if _, err := mk("topics: [1, 2]\n"); err == nil {
		t.Errorf("неверный тип topics должен давать ошибку")
	}
}

// --- вспомогательные ---

// newHandler собирает обработчик через New из реальной роли (как это делает агент).
func newHandler(t *testing.T, st store.Store, clock func() time.Time) *Handler {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "roles", "feedback.md"))
	if err != nil {
		t.Fatal(err)
	}
	r, err := role.Parse(string(src), []string{"feedback"})
	if err != nil {
		t.Fatal(err)
	}
	inst, err := New(core.Deps{AgentID: "feedback-test", Step: "feedback", Store: st,
		Clock: core.Clock(clock), Loc: time.UTC, Sink: nil,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil))}, r.Params)
	if err != nil {
		t.Fatal(err)
	}
	return inst.Tasks.(*Handler)
}

func payload(t *testing.T, m map[string]any) json.RawMessage {
	t.Helper()
	data, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
