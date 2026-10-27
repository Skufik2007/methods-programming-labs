package orders

import (
	"testing"
	"time"

	"github.com/go-playground/validator/v10"
)

func newValidator(t *testing.T) *validator.Validate {
	t.Helper()
	v := validator.New()
	v.SetTagName("binding") // как в Gin
	if err := RegisterValidators(v); err != nil {
		t.Fatal(err)
	}
	return v
}

// fixedNow фиксирует «сегодня» = 2026-10-09.
func fixedNow(t *testing.T) {
	t.Helper()
	orig := Now
	Now = func() time.Time { return time.Date(2026, 10, 9, 15, 0, 0, 0, time.UTC) }
	t.Cleanup(func() { Now = orig })
}

func validRequest() CreateRequest {
	return CreateRequest{
		Customer: Customer{Name: "Иван Петров", Email: "ivan@example.com", Phone: "+79991234567"},
		Items: []Item{
			{SKU: "ABC-123", Quantity: 2, Price: 199.99},
			{SKU: "XYZ-000777", Quantity: 1, Price: 10},
		},
		Delivery: Delivery{Address: "Москва, ул. Пушкина, 1", Date: "2026-10-15"},
	}
}

func TestValidRequest(t *testing.T) {
	fixedNow(t)
	if err := newValidator(t).Struct(validRequest()); err != nil {
		t.Fatalf("валидный запрос отклонён: %v", Describe(err))
	}
}

func TestValidationRules(t *testing.T) {
	fixedNow(t)
	cases := []struct {
		name   string
		mutate func(*CreateRequest)
		field  string
		rule   string
	}{
		{"нет позиций", func(r *CreateRequest) { r.Items = nil }, "items", "required"},
		{"короткое имя", func(r *CreateRequest) { r.Customer.Name = "И" }, "customer.name", "min"},
		{"плохой email", func(r *CreateRequest) { r.Customer.Email = "ivan@" }, "customer.email", "email"},
		{"email с пустой меткой домена", func(r *CreateRequest) { r.Customer.Email = "ivan@ex..com" }, "customer.email", "email"},
		{"email без точки в домене", func(r *CreateRequest) { r.Customer.Email = "ivan@localhost" }, "customer.email", "email"},
		{"имя из пробелов", func(r *CreateRequest) { r.Customer.Name = "    " }, "customer.name", "notblank"},
		{"адрес из пробелов", func(r *CreateRequest) { r.Delivery.Address = "\t      " }, "delivery.address", "notblank"},
		{"цена больше миллиона", func(r *CreateRequest) { r.Items[0].Price = 1_000_000.01 }, "items[0].price", "max"},
		{"плохой телефон", func(r *CreateRequest) { r.Customer.Phone = "8-999-123" }, "customer.phone", "e164"},
		{"SKU строчными", func(r *CreateRequest) { r.Items[0].SKU = "abc-123" }, "items[0].sku", "sku"},
		{"SKU без цифр", func(r *CreateRequest) { r.Items[1].SKU = "ABC-" }, "items[1].sku", "sku"},
		{"количество 0", func(r *CreateRequest) { r.Items[0].Quantity = 0 }, "items[0].quantity", "min"},
		{"количество 101", func(r *CreateRequest) { r.Items[0].Quantity = 101 }, "items[0].quantity", "max"},
		{"отрицательная цена", func(r *CreateRequest) { r.Items[0].Price = -1 }, "items[0].price", "gt"},
		{"три знака в цене", func(r *CreateRequest) { r.Items[0].Price = 1.999 }, "items[0].price", "money"},
		{"дата в прошлом", func(r *CreateRequest) { r.Delivery.Date = "2026-10-08" }, "delivery.date", "delivery_date"},
		{"дата слишком далеко", func(r *CreateRequest) { r.Delivery.Date = "2027-01-08" }, "delivery.date", "delivery_date"},
		{"дата не по формату", func(r *CreateRequest) { r.Delivery.Date = "15.10.2026" }, "delivery.date", "delivery_date"},
		{"повтор SKU", func(r *CreateRequest) { r.Items[1].SKU = "ABC-123" }, "items[1].sku", "unique_sku"},
		{"длинный комментарий", func(r *CreateRequest) { r.Comment = string(make([]byte, 501)) }, "comment", "max"},
	}
	v := newValidator(t)
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req := validRequest()
			c.mutate(&req)
			errs := Describe(v.Struct(req))
			for _, e := range errs {
				if e.Field == c.field && e.Rule == c.rule {
					if e.Message == "" {
						t.Errorf("пустое сообщение для %s", c.field)
					}
					return
				}
			}
			t.Fatalf("ожидалась ошибка %s/%s, получено %+v", c.field, c.rule, errs)
		})
	}
}

func TestDeliveryDateBoundaries(t *testing.T) {
	fixedNow(t)
	for date, want := range map[string]bool{
		"2026-10-09": true,  // сегодня
		"2027-01-07": true,  // ровно +90 дней
		"2027-01-08": false, // +91 день
		"2026-10-08": false, // вчера
		"2026-02-30": false, // несуществующая дата
	} {
		if got := validDeliveryDate(date); got != want {
			t.Errorf("validDeliveryDate(%s) = %v, want %v", date, got, want)
		}
	}
}

// «Сегодня» берётся по UTC: в 01:00 по Москве (22:00 UTC) сегодняшней по UTC
// ещё считается вчерашняя дата — так же, как в Python-сервисе.
func TestDeliveryDateUsesUTC(t *testing.T) {
	orig := Now
	msk := time.FixedZone("MSK", 3*60*60)
	Now = func() time.Time { return time.Date(2026, 10, 10, 1, 0, 0, 0, msk) }
	t.Cleanup(func() { Now = orig })

	if !validDeliveryDate("2026-10-09") {
		t.Fatal("по UTC сейчас 2026-10-09, эта дата должна считаться сегодняшней")
	}
}

func TestValidEmails(t *testing.T) {
	v := newValidator(t)
	for _, email := range []string{"ivan@example.com", "i.van+tag@sub.example.co", "a@b.cd", "o'neil@example.org"} {
		req := validRequest()
		req.Customer.Email = email
		fixedNow(t)
		if err := v.Struct(req); err != nil {
			t.Errorf("%s отклонён: %v", email, Describe(err))
		}
	}
}

func TestTotalCents(t *testing.T) {
	// 0.1 + 0.2 во float даёт 0.30000000000000004 — в копейках ошибки нет.
	items := []Item{{Price: 0.1, Quantity: 1}, {Price: 0.2, Quantity: 1}, {Price: 199.99, Quantity: 3}}
	if got := TotalCents(items); got != 60027 {
		t.Fatalf("TotalCents = %d, want 60027", got)
	}
}

func TestDescribeNonValidationError(t *testing.T) {
	if Describe(nil) != nil {
		t.Fatal("для nil ожидался nil")
	}
}

func BenchmarkValidate(b *testing.B) {
	v := validator.New()
	v.SetTagName("binding")
	if err := RegisterValidators(v); err != nil {
		b.Fatal(err)
	}
	req := validRequest()
	req.Delivery.Date = time.Now().AddDate(0, 0, 3).Format(time.DateOnly)
	b.ReportAllocs()
	for b.Loop() {
		if err := v.Struct(req); err != nil {
			b.Fatal(err)
		}
	}
}
