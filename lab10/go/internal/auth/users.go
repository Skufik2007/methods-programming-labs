package auth

import (
	"errors"
	"fmt"

	"golang.org/x/crypto/bcrypt"
)

var ErrInvalidCredentials = errors.New("неверное имя пользователя или пароль")

type user struct {
	hash []byte
	role string
}

// Users — учётные записи с паролями в виде bcrypt-хешей.
type Users struct {
	byName map[string]user
	dummy  []byte
}

// UserSpec — описание пользователя при запуске сервиса.
type UserSpec struct {
	Name, Password, Role string
}

func NewUsers(specs []UserSpec) (*Users, error) {
	u := &Users{byName: make(map[string]user, len(specs))}
	for _, s := range specs {
		hash, err := bcrypt.GenerateFromPassword([]byte(s.Password), bcrypt.DefaultCost)
		if err != nil {
			return nil, fmt.Errorf("хеш пароля %s: %w", s.Name, err)
		}
		u.byName[s.Name] = user{hash: hash, role: s.Role}
	}
	dummy, err := bcrypt.GenerateFromPassword([]byte("dummy-password"), bcrypt.DefaultCost)
	if err != nil {
		return nil, err
	}
	u.dummy = dummy
	return u, nil
}

// Authenticate возвращает роль пользователя. Для несуществующего имени тоже
// выполняется сравнение bcrypt, чтобы по времени ответа нельзя было понять,
// есть ли такой пользователь.
func (u *Users) Authenticate(name, password string) (string, error) {
	rec, ok := u.byName[name]
	hash := rec.hash
	if !ok {
		hash = u.dummy
	}
	if err := bcrypt.CompareHashAndPassword(hash, []byte(password)); err != nil || !ok {
		return "", ErrInvalidCredentials
	}
	return rec.role, nil
}
