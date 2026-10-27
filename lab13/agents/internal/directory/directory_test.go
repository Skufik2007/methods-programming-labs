package directory

import "testing"

func TestDirectory(t *testing.T) {
	t.Parallel()
	codes := Codes()
	if len(codes) != 7 {
		t.Fatalf("специальностей %d, ожидалось 7 (раздел 7)", len(codes))
	}
	for _, c := range codes {
		if !Valid(c) || Title(c) == c || Dative(c) == c {
			t.Errorf("%s: нет названия или падежа", c)
		}
	}
	if Valid("dentist") || Valid("") {
		t.Error("вне справочника — не валидно")
	}
	if Title("dentist") != "dentist" || Dative("dentist") != "dentist" {
		t.Error("для неизвестного кода название — сам код")
	}
	if Dative("therapist") != "терапевту" || Title("ent") != "ЛОР" {
		t.Errorf("названия: %q %q", Dative("therapist"), Title("ent"))
	}
	if !Valid(Default) {
		t.Errorf("Default %q не в справочнике", Default)
	}
}
