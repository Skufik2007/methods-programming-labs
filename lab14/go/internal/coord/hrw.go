// Пакет coord — координация сборщиков через etcd (docs/ARCHITECTURE.md, раздел 5):
// членство под lease, распределение устройств по rendezvous hashing и чекпоинты.
//
// Этот файл — чистая логика без сети: её можно проверять тестом отдельно от etcd.
package coord

import (
	"hash/fnv"
	"sort"
)

// Weight — вес пары (сборщик, устройство): fnv64a(collector_id + "/" + device_id), затем
// финализатор murmur3 fmix64. Без финализатора fnv64a для строк с общим суффиксом даёт
// почти одинаковый порядок весов (сборщик с большим id выигрывал все устройства), и
// HRW вырождается; fmix64 лавинно перемешивает биты.
func Weight(collectorID, deviceID string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(collectorID))
	_, _ = h.Write([]byte{'/'})
	_, _ = h.Write([]byte(deviceID))
	return fmix64(h.Sum64())
}

func fmix64(k uint64) uint64 {
	k ^= k >> 33
	k *= 0xff51afd7ed558ccd
	k ^= k >> 33
	k *= 0xc4ceb9fe1a85ec53
	k ^= k >> 33
	return k
}

// Owner возвращает владельца устройства среди members (максимальный вес; при равенстве
// весов — меньший по порядку id, чтобы результат не зависел от порядка в списке).
// Для пустого списка возвращает "".
func Owner(members []string, deviceID string) string {
	var best string
	var bestW uint64
	for i, m := range members {
		w := Weight(m, deviceID)
		if i == 0 || w > bestW || (w == bestW && m < best) {
			best, bestW = m, w
		}
	}
	return best
}

// Assign возвращает отсортированный список устройств, которыми владеет self.
// Если self нет среди members, список пуст: сборщик без регистрации ничего не опрашивает.
func Assign(members []string, devices []string, self string) []string {
	present := false
	for _, m := range members {
		if m == self {
			present = true
			break
		}
	}
	if !present {
		return nil
	}
	var out []string
	for _, d := range devices {
		if Owner(members, d) == self {
			out = append(out, d)
		}
	}
	sort.Strings(out)
	return out
}

// Without возвращает копию members без id.
func Without(members []string, id string) []string {
	out := make([]string, 0, len(members))
	for _, m := range members {
		if m != id {
			out = append(out, m)
		}
	}
	return out
}
