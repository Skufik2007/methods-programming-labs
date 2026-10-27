package orders

import (
	"errors"
	"slices"
	"sync"
	"time"

	"github.com/google/uuid"
)

var (
	ErrNotFound  = errors.New("заказ не найден")
	ErrForbidden = errors.New("заказ принадлежит другому пользователю")
)

// Store — потокобезопасное хранилище заказов в памяти.
type Store struct {
	mu     sync.RWMutex
	orders map[string]Order
}

func NewStore() *Store {
	return &Store{orders: make(map[string]Order)}
}

func (s *Store) Create(owner string, req CreateRequest) Order {
	cents := TotalCents(req.Items)
	o := Order{
		ID:         uuid.NewString(),
		Owner:      owner,
		Customer:   req.Customer,
		Items:      slices.Clone(req.Items),
		Delivery:   req.Delivery,
		Comment:    req.Comment,
		TotalCents: cents,
		Total:      float64(cents) / 100,
		CreatedAt:  time.Now().UTC(),
	}
	s.mu.Lock()
	s.orders[o.ID] = o
	s.mu.Unlock()
	return o
}

// Get возвращает заказ, только если он принадлежит owner.
func (s *Store) Get(owner, id string) (Order, error) {
	s.mu.RLock()
	o, ok := s.orders[id]
	s.mu.RUnlock()
	switch {
	case !ok:
		return Order{}, ErrNotFound
	case o.Owner != owner:
		return Order{}, ErrForbidden
	}
	return o, nil
}

// List возвращает заказы владельца, от новых к старым.
func (s *Store) List(owner string) []Order {
	s.mu.RLock()
	out := make([]Order, 0)
	for _, o := range s.orders {
		if o.Owner == owner {
			out = append(out, o)
		}
	}
	s.mu.RUnlock()
	slices.SortFunc(out, func(a, b Order) int { return b.CreatedAt.Compare(a.CreatedAt) })
	return out
}
