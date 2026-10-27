package auth

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
)

var (
	keyOnce sync.Once
	testKey *rsa.PrivateKey
)

// Генерация RSA-ключа медленная, поэтому один ключ на все тесты.
func key(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	keyOnce.Do(func() {
		k, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			t.Fatal(err)
		}
		testKey = k
	})
	return testKey
}

var cfg = Config{Issuer: "lab10-go-api", Audience: "lab10", TTL: time.Minute}

func newIssuer(t *testing.T, c Config, k *rsa.PrivateKey) *Issuer {
	t.Helper()
	i, err := NewIssuer(c, k)
	if err != nil {
		t.Fatal(err)
	}
	return i
}

func TestIssueVerify(t *testing.T) {
	i := newIssuer(t, cfg, key(t))
	tok, exp, err := i.Issue("alice", "user")
	if err != nil {
		t.Fatal(err)
	}
	if time.Until(exp) <= 0 {
		t.Fatal("exp в прошлом")
	}
	c, err := i.Verify(tok)
	if err != nil {
		t.Fatal(err)
	}
	if c.Subject != "alice" || c.Role != "user" || c.ID == "" {
		t.Fatalf("claims: %+v", c)
	}
}

func TestVerifyRejects(t *testing.T) {
	i := newIssuer(t, cfg, key(t))
	good, _, _ := i.Issue("alice", "user")

	sign := func(c Claims, m jwt.SigningMethod, k any, kid string) string {
		tok := jwt.NewWithClaims(m, c)
		if kid != "" {
			tok.Header["kid"] = kid
		}
		s, err := tok.SignedString(k)
		if err != nil {
			t.Fatal(err)
		}
		return s
	}
	base := func() Claims {
		now := time.Now()
		return Claims{Role: "user", RegisteredClaims: jwt.RegisteredClaims{
			Issuer: cfg.Issuer, Subject: "alice", Audience: jwt.ClaimStrings{cfg.Audience},
			IssuedAt: jwt.NewNumericDate(now), ExpiresAt: jwt.NewNumericDate(now.Add(time.Minute)),
		}}
	}
	otherKey, _ := rsa.GenerateKey(rand.Reader, 2048)

	expired := base()
	expired.ExpiresAt = jwt.NewNumericDate(time.Now().Add(-time.Hour))
	wrongAud := base()
	wrongAud.Audience = jwt.ClaimStrings{"other"}
	wrongIss := base()
	wrongIss.Issuer = "evil"
	noExp := base()
	noExp.ExpiresAt = nil
	noSub := base()
	noSub.Subject = ""

	cases := map[string]string{
		"истёк":            sign(expired, jwt.SigningMethodRS256, key(t), i.kid),
		"чужой audience":   sign(wrongAud, jwt.SigningMethodRS256, key(t), i.kid),
		"чужой issuer":     sign(wrongIss, jwt.SigningMethodRS256, key(t), i.kid),
		"без exp":          sign(noExp, jwt.SigningMethodRS256, key(t), i.kid),
		"без sub":          sign(noSub, jwt.SigningMethodRS256, key(t), i.kid),
		"чужой ключ":       sign(base(), jwt.SigningMethodRS256, otherKey, i.kid),
		"неизвестный kid":  sign(base(), jwt.SigningMethodRS256, key(t), "other"),
		"alg=none":         sign(base(), jwt.SigningMethodNone, jwt.UnsafeAllowNoneSignatureType, i.kid),
		"подпись изменена": good[:len(good)-4] + "AAAA",
		"мусор":            "not.a.jwt",
		"HS256 с открытым ключом": sign(base(), jwt.SigningMethodHS256,
			[]byte(base64.StdEncoding.EncodeToString(key(t).PublicKey.N.Bytes())), i.kid),
	}
	for name, tok := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := i.Verify(tok); err == nil {
				t.Fatal("токен должен быть отклонён")
			}
		})
	}
}

// JWKS должен позволять проверить подпись — именно так это делает Python-сервис.
func TestJWKSVerifiesToken(t *testing.T) {
	i := newIssuer(t, cfg, key(t))
	tok, _, _ := i.Issue("alice", "user")

	jwk := i.JWKS().Keys[0]
	if jwk.Kty != "RSA" || jwk.Alg != "RS256" || jwk.Kid == "" {
		t.Fatalf("jwk: %+v", jwk)
	}
	n, _ := base64.RawURLEncoding.DecodeString(jwk.N)
	e, _ := base64.RawURLEncoding.DecodeString(jwk.E)
	pub := &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(new(big.Int).SetBytes(e).Int64())}

	parsed, err := jwt.Parse(tok, func(*jwt.Token) (any, error) { return pub, nil },
		jwt.WithValidMethods([]string{"RS256"}))
	if err != nil || !parsed.Valid {
		t.Fatalf("подпись не проверяется ключом из JWKS: %v", err)
	}
}

func TestKeyIDStable(t *testing.T) {
	a := newIssuer(t, cfg, key(t))
	b := newIssuer(t, cfg, key(t))
	if a.kid != b.kid || len(a.kid) != 16 {
		t.Fatalf("kid %q vs %q", a.kid, b.kid)
	}
}

func TestLoadOrCreateKey(t *testing.T) {
	path := filepath.Join(t.TempDir(), "jwt.pem")

	first, created, err := LoadOrCreateKey(path)
	if err != nil || !created {
		t.Fatalf("создание: created=%v err=%v", created, err)
	}
	if runtime.GOOS != "windows" {
		if st, _ := os.Stat(path); st.Mode().Perm() != 0o600 {
			t.Fatalf("права файла ключа %v, ожидалось 0600", st.Mode().Perm())
		}
	}

	// Повторный запуск — тот же ключ, поэтому выданные токены остаются валидными.
	second, created, err := LoadOrCreateKey(path)
	if err != nil || created {
		t.Fatalf("загрузка: created=%v err=%v", created, err)
	}
	a, b := newIssuer(t, cfg, first), newIssuer(t, cfg, second)
	tok, _, _ := a.Issue("alice", "user")
	if _, err := b.Verify(tok); err != nil {
		t.Fatalf("токен до перезапуска не принят после: %v", err)
	}

	entries, _ := os.ReadDir(filepath.Dir(path))
	if len(entries) != 1 {
		t.Fatalf("временные файлы остались: %d файлов", len(entries))
	}
}

func TestLoadOrCreateKeyRejectsCorruptedFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "jwt.pem")
	if err := os.WriteFile(path, []byte("not a pem"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := LoadOrCreateKey(path); err == nil {
		t.Fatal("испорченный ключ должен давать ошибку, а не молча заменяться")
	}
	if data, _ := os.ReadFile(path); string(data) != "not a pem" {
		t.Fatal("испорченный файл не должен перезаписываться")
	}
}

func TestUsers(t *testing.T) {
	u, err := NewUsers([]UserSpec{{Name: "alice", Password: "secret-pass", Role: "admin"}})
	if err != nil {
		t.Fatal(err)
	}
	if role, err := u.Authenticate("alice", "secret-pass"); err != nil || role != "admin" {
		t.Fatalf("верный пароль: %q %v", role, err)
	}
	if _, err := u.Authenticate("alice", "wrong"); err == nil {
		t.Fatal("неверный пароль принят")
	}
	if _, err := u.Authenticate("bob", "secret-pass"); err == nil {
		t.Fatal("несуществующий пользователь принят")
	}
}

func TestMiddleware(t *testing.T) {
	gin.SetMode(gin.TestMode)
	i := newIssuer(t, cfg, key(t))
	r := gin.New()
	r.GET("/me", Required(i), func(c *gin.Context) { c.String(200, CurrentUser(c).Subject) })

	tok, _, _ := i.Issue("alice", "user")
	cases := []struct {
		header string
		status int
	}{
		{"", 401},
		{"Basic abc", 401},
		{"Bearer", 401},
		{"Bearer garbage", 401},
		{"Bearer " + tok, 200},
		{"bearer " + tok, 200}, // схема без учёта регистра (RFC 7235)
	}
	for _, c := range cases {
		req := httptest.NewRequest(http.MethodGet, "/me", nil)
		if c.header != "" {
			req.Header.Set("Authorization", c.header)
		}
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != c.status {
			t.Errorf("%q: status %d, want %d", c.header, w.Code, c.status)
		}
		if w.Code == 401 && !strings.HasPrefix(w.Header().Get("WWW-Authenticate"), "Bearer") {
			t.Errorf("%q: нет WWW-Authenticate", c.header)
		}
		if w.Code == 200 && w.Body.String() != "alice" {
			t.Errorf("subject %q", w.Body.String())
		}
	}
}
