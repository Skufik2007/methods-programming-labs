// Пакет autoscale — автомасштабировщик экземпляров агентов (раздел 12 контракта).
// Политика (policy.go) — чистая функция; наблюдение, бэкенды и цикл вынесены в интерфейсы.
package autoscale

import "time"

// Params — параметры политики одного шага.
type Params struct {
	PerInstance int           // сколько задач в очереди «обслуживает» один экземпляр
	Min         int           // экземпляры, запущенные вне автомасштабировщика (он ими не управляет)
	Max         int           // верхняя граница общего числа экземпляров (статические + добавочные)
	Cooldown    time.Duration // сколько ждать без роста очереди перед уменьшением
}

// Desired возвращает желаемое ОБЩЕЕ число экземпляров шага (min статических + добавочные).
//
//	backlog    — num_pending + num_ack_pending консьюмера;
//	current    — текущее общее число экземпляров;
//	lastGrowth — момент последнего роста очереди (или последнего увеличения числа экземпляров).
//
// Рост — сразу и на сколько нужно; уменьшение — не раньше чем через Cooldown после
// lastGrowth и не ниже Min. Превышение Max исправляется сразу.
func Desired(backlog, current int, lastGrowth, now time.Time, p Params) int {
	if backlog < 0 {
		backlog = 0
	}
	per := p.PerInstance
	if per < 1 {
		per = 1
	}
	target := (backlog + per - 1) / per // ceil(backlog / per)
	if target < p.Min {
		target = p.Min
	}
	if p.Max > 0 && target > p.Max {
		target = p.Max
	}
	if target >= current {
		return target
	}
	// target < current: уменьшение.
	if now.Sub(lastGrowth) >= p.Cooldown {
		return target
	}
	// Cooldown не истёк: держим текущее число, но не выше Max.
	if p.Max > 0 && current > p.Max {
		return p.Max
	}
	return current
}
