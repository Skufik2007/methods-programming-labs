package core

import (
	"bytes"
	"encoding/json"
)

// Payload — «слоистый» доступ к payload задачи: сначала верхний уровень, затем case,
// затем previous. Нужен там, где контракт не фиксирует, в каком слое лежит поле.
type Payload struct {
	layers []map[string]json.RawMessage
}

func ParsePayload(raw json.RawMessage) (Payload, error) {
	var top map[string]json.RawMessage
	if len(bytes.TrimSpace(raw)) == 0 {
		return Payload{}, nil
	}
	if err := json.Unmarshal(raw, &top); err != nil {
		return Payload{}, err
	}
	p := Payload{layers: []map[string]json.RawMessage{top}}
	for _, name := range []string{"case", "previous"} {
		var sub map[string]json.RawMessage
		if v, ok := top[name]; ok && json.Unmarshal(v, &sub) == nil && sub != nil {
			p.layers = append(p.layers, sub)
		} else {
			p.layers = append(p.layers, nil)
		}
	}
	return p, nil
}

// Layer возвращает слой: 0 — верхний, 1 — case, 2 — previous.
func (p Payload) Layer(i int) map[string]json.RawMessage {
	if i < 0 || i >= len(p.layers) {
		return nil
	}
	return p.layers[i]
}

// Get ищет ключ по слоям в порядке order (по умолчанию 0,1,2) и декодирует в dst.
func (p Payload) Get(key string, dst any, order ...int) bool {
	if len(order) == 0 {
		order = []int{0, 1, 2}
	}
	for _, i := range order {
		if v, ok := p.Layer(i)[key]; ok && string(bytes.TrimSpace(v)) != "null" {
			if json.Unmarshal(v, dst) == nil {
				return true
			}
		}
	}
	return false
}
