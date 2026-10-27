// Package orders описывает заказ: входную модель с правилами валидации,
// хранилище и расчёт суммы.
package orders

import "time"

// CreateRequest — тело запроса на создание заказа.
//
// Правила валидации заданы тегами `binding` (go-playground/validator, который
// использует Gin). Нестандартные правила (sku, money, delivery_date) и
// проверка уникальности SKU регистрируются в RegisterValidators.
type CreateRequest struct {
	Customer Customer `json:"customer"`
	Items    []Item   `json:"items" binding:"required,min=1,max=50,dive"`
	Delivery Delivery `json:"delivery"`
	Comment  string   `json:"comment" binding:"max=500"`
}

type Customer struct {
	Name  string `json:"name" binding:"required,notblank,min=2,max=100"`
	Email string `json:"email" binding:"required,max=254,email"`
	Phone string `json:"phone" binding:"omitempty,e164"`
}

type Item struct {
	SKU      string  `json:"sku" binding:"required,sku"`
	Quantity int     `json:"quantity" binding:"min=1,max=100"`
	Price    float64 `json:"price" binding:"gt=0,max=1000000,money"`
}

type Delivery struct {
	Address string `json:"address" binding:"required,notblank,min=5,max=300"`
	// Дата в формате YYYY-MM-DD: не раньше сегодняшнего дня и не дальше MaxDeliveryDays.
	Date string `json:"date" binding:"required,delivery_date"`
}

// Order — сохранённый заказ.
type Order struct {
	ID         string    `json:"id"`
	Owner      string    `json:"owner"`
	Customer   Customer  `json:"customer"`
	Items      []Item    `json:"items"`
	Delivery   Delivery  `json:"delivery"`
	Comment    string    `json:"comment,omitempty"`
	TotalCents int64     `json:"total_cents"`
	Total      float64   `json:"total"`
	CreatedAt  time.Time `json:"created_at"`
}

// TotalCents считает сумму заказа в копейках, чтобы не накапливать ошибку float.
func TotalCents(items []Item) int64 {
	var total int64
	for _, it := range items {
		total += toCents(it.Price) * int64(it.Quantity)
	}
	return total
}
