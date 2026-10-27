// Пакет core — общие типы контракта (задача, результат, событие) и интерфейсы,
// через которые виды агентов подключаются к шине, не зная про NATS.
package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/Skufik2007/methods-programming-lab13/agents/internal/store"
	"github.com/google/uuid"
)

// Clock — инжектируемые часы.
type Clock func() time.Time

func (c Clock) Now() time.Time {
	if c == nil {
		return time.Now().UTC()
	}
	return c().UTC()
}

// Task — сообщение из потока TASKS (раздел 4.1).
type Task struct {
	TaskID    string          `json:"task_id"`
	RunID     string          `json:"run_id"`
	Step      string          `json:"step"`
	Kind      string          `json:"kind"`
	Attempt   int             `json:"attempt"`
	CreatedAt string          `json:"created_at"`
	Payload   json.RawMessage `json:"payload"`
}

// Result — результат шага (раздел 4.2). Retryable — расширение из раздела 5.
type Result struct {
	TaskID     string `json:"task_id"`
	RunID      string `json:"run_id"`
	Step       string `json:"step"`
	AgentID    string `json:"agent_id"`
	Success    bool   `json:"success"`
	Output     any    `json:"output"`
	Error      string `json:"error"`
	Retryable  bool   `json:"retryable,omitempty"`
	DurationMS int64  `json:"duration_ms"`
	FinishedAt string `json:"finished_at"`
}

// Event — запись журнала (раздел 10).
type Event struct {
	TS      string `json:"ts"`
	Source  string `json:"source"`
	Action  string `json:"action"`
	RunID   string `json:"run_id"`
	TaskID  string `json:"task_id"`
	Step    string `json:"step"`
	TraceID string `json:"trace_id"`
	SpanID  string `json:"span_id"`
	Subject string `json:"subject"`
	Summary string `json:"summary"`
}

// Failure — ошибка обработки с признаком «временная».
type Failure struct {
	Msg       string
	Retryable bool
}

func (f *Failure) Error() string { return f.Msg }

// Permanent — бизнес-ошибка, повтор не поможет.
func Permanent(format string, a ...any) error { return &Failure{Msg: fmt.Sprintf(format, a...)} }

// Transient — временный сбой (Redis, сеть): оркестратор может повторить.
func Transient(format string, a ...any) error {
	return &Failure{Msg: fmt.Sprintf(format, a...), Retryable: true}
}

// IsRetryable: неизвестные ошибки считаем временными.
func IsRetryable(err error) bool {
	var f *Failure
	if errors.As(err, &f) {
		return f.Retryable
	}
	return true
}

// Sink — исходящая сторона шины для бизнес-логики.
type Sink interface {
	// Emit публикует событие в events.<agent_id>; ошибки не возвращает (журнал вторичен).
	Emit(ctx context.Context, e Event)
	Publish(ctx context.Context, subject string, v any) error
}

// TaskHandler обрабатывает задачу из потока и возвращает output.
type TaskHandler interface {
	Handle(ctx context.Context, t Task) (any, error)
}

// Subscription — подписка core NATS (аукцион, бронь). Handle возвращает ответ
// (nil — не отвечать) и признак ошибки для счётчика failed.
type Subscription struct {
	Subject string
	Span    string
	Handle  func(ctx context.Context, data []byte) (reply []byte, failed bool)
}

// Instance — что вид агента отдаёт запускающему коду.
type Instance struct {
	Tasks         TaskHandler // nil — вид не читает поток TASKS (booking)
	Subscriptions []Subscription
	Background    []func(ctx context.Context) // фоновые циклы (диспетчеры)
}

// Deps — зависимости, которые main передаёт конструкторам видов.
type Deps struct {
	AgentID string
	Step    string
	Store   store.Store
	Clock   Clock
	Loc     *time.Location
	Sink    Sink
	Log     *slog.Logger

	ReminderDemoDelay time.Duration
	SurveyDemoDelay   time.Duration
}

// StableID — детерминированный UUID (v5) из частей: повторная обработка одной и той же
// записи даёт те же идентификаторы напоминаний и опросов, и дубли не создаются.
func StableID(parts ...string) string {
	return uuid.NewSHA1(uuid.NameSpaceURL, []byte("mas:"+strings.Join(parts, "|"))).String()
}
