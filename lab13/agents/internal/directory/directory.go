// Пакет directory — справочник специальностей (раздел 7 контракта).
package directory

type entry struct{ code, title, dative string }

var entries = []entry{
	{"therapist", "Терапевт", "терапевту"},
	{"cardiologist", "Кардиолог", "кардиологу"},
	{"neurologist", "Невролог", "неврологу"},
	{"ophthalmologist", "Офтальмолог", "офтальмологу"},
	{"dermatologist", "Дерматолог", "дерматологу"},
	{"ent", "ЛОР", "ЛОРу"},
	{"traumatologist", "Травматолог", "травматологу"},
}

const Default = "therapist"

func Valid(code string) bool {
	for _, e := range entries {
		if e.code == code {
			return true
		}
	}
	return false
}

func Title(code string) string {
	for _, e := range entries {
		if e.code == code {
			return e.title
		}
	}
	return code
}

// Dative — название в дательном падеже («запись к терапевту»).
func Dative(code string) string {
	for _, e := range entries {
		if e.code == code {
			return e.dative
		}
	}
	return code
}

func Codes() []string {
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = e.code
	}
	return out
}
