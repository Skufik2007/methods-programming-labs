package autoscale

import (
	"context"
	"fmt"
)

// Instance — добавочный экземпляр агента, которым управляет автомасштабировщик.
type Instance struct {
	ID   string // AGENT_ID: <step>-auto-<n>
	Step string
	Seq  int // номер n; при уменьшении убираем самые новые
}

// Backend запускает и останавливает добавочные экземпляры.
type Backend interface {
	// List возвращает работающие добавочные экземпляры шага, по возрастанию Seq.
	// Завершившиеся (в т.ч. упавшие) в список не попадают.
	List(ctx context.Context, step string) ([]Instance, error)
	Start(ctx context.Context, step StepConfig) (Instance, error)
	Stop(ctx context.Context, inst Instance) error
}

func instanceID(step string, seq int) string { return fmt.Sprintf("%s-auto-%d", step, seq) }
