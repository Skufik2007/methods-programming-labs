package auth

import (
	"sync"
	"time"
)

// LoginLimiter ограничивает подбор паролей: после MaxFailures неудачных входов
// подряд ключ (имя пользователя + IP клиента) блокируется на Lockout с момента
// первой неудачи. Успешный вход сбрасывает счётчик.
//
// Ключ включает IP, чтобы злоумышленник не мог заблокировать чужую учётную
// запись со своего адреса. Хранение в памяти — для одного экземпляра сервиса;
// при нескольких экземплярах счётчики пришлось бы вынести в общее хранилище.
type LoginLimiter struct {
	mu          sync.Mutex
	maxFailures int
	lockout     time.Duration
	now         func() time.Time
	entries     map[string]*attempts
}

type attempts struct {
	failures int
	since    time.Time
}

// maxEntries — после стольких записей при очередной неудаче удаляются устаревшие.
const maxEntries = 10_000

func NewLoginLimiter(maxFailures int, lockout time.Duration) *LoginLimiter {
	return &LoginLimiter{
		maxFailures: maxFailures,
		lockout:     lockout,
		now:         time.Now,
		entries:     make(map[string]*attempts),
	}
}

// Allow сообщает, можно ли сейчас пробовать войти, и через сколько повторить, если нет.
func (l *LoginLimiter) Allow(key string) (bool, time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()
	a, ok := l.entries[key]
	if !ok {
		return true, 0
	}
	elapsed := l.now().Sub(a.since)
	if elapsed >= l.lockout {
		delete(l.entries, key)
		return true, 0
	}
	if a.failures >= l.maxFailures {
		return false, l.lockout - elapsed
	}
	return true, 0
}

// Failure учитывает неудачную попытку.
func (l *LoginLimiter) Failure(key string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	a, ok := l.entries[key]
	if !ok || now.Sub(a.since) >= l.lockout {
		a = &attempts{since: now}
		l.entries[key] = a
	}
	a.failures++
	if len(l.entries) > maxEntries {
		for k, v := range l.entries {
			if now.Sub(v.since) >= l.lockout {
				delete(l.entries, k)
			}
		}
	}
}

// Success сбрасывает счётчик после успешного входа.
func (l *LoginLimiter) Success(key string) {
	l.mu.Lock()
	delete(l.entries, key)
	l.mu.Unlock()
}
