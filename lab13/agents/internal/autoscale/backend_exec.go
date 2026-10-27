package autoscale

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"sort"
	"sync"
	"syscall"
	"time"
)

// ExecBackend запускает агентов как локальные процессы.
type ExecBackend struct {
	bin         string
	stopTimeout time.Duration
	log         *slog.Logger

	mu    sync.Mutex
	next  int
	procs map[string]*execProc
}

type execProc struct {
	inst Instance
	cmd  *exec.Cmd
	done chan struct{} // закрывается, когда процесс завершился
}

func NewExecBackend(c ExecConfig, log *slog.Logger) *ExecBackend {
	return &ExecBackend{bin: c.AgentBin, stopTimeout: c.StopTimeout, log: log, procs: map[string]*execProc{}}
}

func (b *ExecBackend) List(_ context.Context, step string) ([]Instance, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []Instance
	for _, p := range b.procs {
		if p.inst.Step == step {
			out = append(out, p.inst)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, nil
}

func (b *ExecBackend) Start(_ context.Context, sc StepConfig) (Instance, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.next++
	inst := Instance{ID: instanceID(sc.Step, b.next), Step: sc.Step, Seq: b.next}
	// Не CommandContext: остановкой управляем сами, чтобы дать агенту доделать задачу.
	cmd := exec.Command(b.bin, "-role", sc.Role)
	cmd.Env = append(os.Environ(), "AGENT_ID="+inst.ID)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		return Instance{}, fmt.Errorf("запуск %s: %w", b.bin, err)
	}
	p := &execProc{inst: inst, cmd: cmd, done: make(chan struct{})}
	b.procs[inst.ID] = p
	// Отслеживаем завершение: упавший процесс пропадает из List, и политика запустит новый.
	go func() {
		err := cmd.Wait()
		b.mu.Lock()
		delete(b.procs, inst.ID)
		b.mu.Unlock()
		close(p.done)
		b.log.Info("процесс агента завершился", "agent_id", inst.ID, "err", err)
	}()
	return inst, nil
}

func (b *ExecBackend) Stop(ctx context.Context, inst Instance) error {
	b.mu.Lock()
	p := b.procs[inst.ID]
	b.mu.Unlock()
	if p == nil {
		return nil // уже завершился
	}
	// Мягкая остановка; на Windows Signal не поддерживается — сразу Kill.
	// Неподтверждённая задача вернётся в очередь после AckWait.
	if err := p.cmd.Process.Signal(syscall.SIGTERM); err != nil {
		b.log.Debug("мягкая остановка недоступна, Kill", "agent_id", inst.ID, "err", err)
		_ = p.cmd.Process.Kill()
	}
	select {
	case <-p.done:
		return nil
	case <-time.After(b.stopTimeout):
		_ = p.cmd.Process.Kill()
	case <-ctx.Done():
		_ = p.cmd.Process.Kill()
	}
	select {
	case <-p.done:
	case <-time.After(2 * time.Second):
		return fmt.Errorf("процесс %s не завершился после Kill", inst.ID)
	}
	return nil
}
