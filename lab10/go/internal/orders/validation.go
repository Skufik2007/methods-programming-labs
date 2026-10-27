package orders

import (
	"errors"
	"fmt"
	"math"
	"reflect"
	"regexp"
	"strings"
	"time"

	"github.com/go-playground/validator/v10"
)

// MaxDeliveryDays — насколько далеко вперёд можно назначить доставку.
const MaxDeliveryDays = 90

var skuPattern = regexp.MustCompile(`^[A-Z]{3}-\d{3,6}$`)

// EmailPattern — шаблон email из стандарта HTML (WHATWG), но с обязательной точкой
// в домене. Встроенное правило email валидатора заменено им, а Python-сервис
// использует тот же шаблон (python/app/models.py) — так оба сервиса гарантированно
// принимают и отклоняют одни и те же адреса.
const EmailPattern = `^[a-zA-Z0-9.!#$%&'*+/=?^_` + "`" + `{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$`

var emailPattern = regexp.MustCompile(EmailPattern)

// Now подменяется в тестах, чтобы проверка даты не зависела от текущего дня.
var Now = time.Now

// RegisterValidators добавляет в валидатор нестандартные правила заказа
// и заставляет его называть поля по JSON-тегам (items[0].sku, а не Items[0].SKU).
func RegisterValidators(v *validator.Validate) error {
	v.RegisterTagNameFunc(func(f reflect.StructField) string {
		name, _, _ := strings.Cut(f.Tag.Get("json"), ",")
		if name == "-" {
			return ""
		}
		return name
	})

	rules := map[string]validator.Func{
		"sku":           func(fl validator.FieldLevel) bool { return skuPattern.MatchString(fl.Field().String()) },
		"email":         func(fl validator.FieldLevel) bool { return emailPattern.MatchString(fl.Field().String()) },
		"notblank":      func(fl validator.FieldLevel) bool { return strings.TrimSpace(fl.Field().String()) != "" },
		"money":         func(fl validator.FieldLevel) bool { return isMoney(fl.Field().Float()) },
		"delivery_date": func(fl validator.FieldLevel) bool { return validDeliveryDate(fl.Field().String()) },
	}
	for tag, fn := range rules {
		if err := v.RegisterValidation(tag, fn); err != nil {
			return fmt.Errorf("register %s: %w", tag, err)
		}
	}
	v.RegisterStructValidation(uniqueSKUs, CreateRequest{})
	return nil
}

// isMoney проверяет, что у цены не больше двух знаков после запятой.
func isMoney(f float64) bool {
	cents := f * 100
	return math.Abs(cents-math.Round(cents)) < 1e-6
}

func toCents(f float64) int64 { return int64(math.Round(f * 100)) }

func validDeliveryDate(s string) bool {
	d, err := time.Parse(time.DateOnly, s)
	if err != nil {
		return false
	}
	// «Сегодня» — по UTC, как и в Python-сервисе. По местному времени сервисы
	// расходились бы ночью: в 01:00 по Москве в UTC ещё вчерашний день.
	now := Now().UTC()
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	return !d.Before(today) && !d.After(today.AddDate(0, 0, MaxDeliveryDays))
}

// uniqueSKUs запрещает повторять один SKU в нескольких позициях:
// количество задаётся полем quantity, а не дублированием строки.
func uniqueSKUs(sl validator.StructLevel) {
	req := sl.Current().Interface().(CreateRequest)
	seen := make(map[string]int, len(req.Items))
	for i, it := range req.Items {
		if !skuPattern.MatchString(it.SKU) {
			continue // о невалидном SKU уже сообщило правило sku, дубль не важен
		}
		if first, dup := seen[it.SKU]; dup {
			sl.ReportError(req.Items[i].SKU, fmt.Sprintf("items[%d].sku", i), "SKU", "unique_sku", fmt.Sprint(first))
			continue
		}
		seen[it.SKU] = i
	}
}

// FieldError — понятное клиенту описание одной ошибки валидации.
type FieldError struct {
	Field   string `json:"field"`
	Rule    string `json:"rule"`
	Message string `json:"message"`
}

// Describe превращает ошибки validator в список FieldError.
// Если err не ошибка валидации, возвращает nil.
func Describe(err error) []FieldError {
	var verrs validator.ValidationErrors
	if !errors.As(err, &verrs) {
		return nil
	}
	out := make([]FieldError, 0, len(verrs))
	for _, fe := range verrs {
		out = append(out, FieldError{
			Field:   fieldPath(fe),
			Rule:    fe.Tag(),
			Message: message(fe),
		})
	}
	return out
}

// fieldPath убирает имя корневой структуры: "CreateRequest.items[0].sku" -> "items[0].sku".
func fieldPath(fe validator.FieldError) string {
	ns := fe.Namespace()
	if fe.Tag() == "unique_sku" {
		return fe.Field() // для struct-level ошибки путь передан целиком
	}
	if _, rest, ok := strings.Cut(ns, "."); ok {
		return rest
	}
	return ns
}

func message(fe validator.FieldError) string {
	switch fe.Tag() {
	case "required":
		return "обязательное поле"
	case "min":
		if fe.Kind() == reflect.String {
			return "минимальная длина " + fe.Param()
		}
		if fe.Kind() == reflect.Slice {
			return "минимум элементов: " + fe.Param()
		}
		return "значение должно быть не меньше " + fe.Param()
	case "max":
		if fe.Kind() == reflect.String {
			return "максимальная длина " + fe.Param()
		}
		if fe.Kind() == reflect.Slice {
			return "максимум элементов: " + fe.Param()
		}
		return "значение должно быть не больше " + fe.Param()
	case "gt":
		return "значение должно быть больше " + fe.Param()
	case "email":
		return "некорректный email"
	case "notblank":
		return "не может состоять только из пробелов"
	case "e164":
		return "телефон в формате E.164, например +79991234567"
	case "sku":
		return "SKU в формате ABC-123 (три заглавные латинские буквы, дефис, 3–6 цифр)"
	case "money":
		return "не больше двух знаков после запятой"
	case "delivery_date":
		return fmt.Sprintf("дата YYYY-MM-DD от сегодняшнего дня до +%d дней", MaxDeliveryDays)
	case "unique_sku":
		return "SKU повторяется (совпадает с позицией items[" + fe.Param() + "])"
	default:
		return "не прошло проверку " + fe.Tag()
	}
}
