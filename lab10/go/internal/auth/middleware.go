package auth

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

const claimsKey = "auth.claims"

// Required пропускает запрос только с валидным токеном в заголовке
// Authorization: Bearer <token>. Ответ при ошибке соответствует RFC 6750.
func Required(issuer *Issuer) gin.HandlerFunc {
	return func(c *gin.Context) {
		scheme, token, ok := strings.Cut(c.GetHeader("Authorization"), " ")
		if !ok || !strings.EqualFold(scheme, "Bearer") || token == "" {
			c.Header("WWW-Authenticate", `Bearer realm="lab10"`)
			abort(c, "unauthorized", "нужен заголовок Authorization: Bearer <token>")
			return
		}
		claims, err := issuer.Verify(strings.TrimSpace(token))
		if err != nil {
			c.Header("WWW-Authenticate", `Bearer realm="lab10", error="invalid_token"`)
			abort(c, "invalid_token", "токен недействителен: "+err.Error())
			return
		}
		c.Set(claimsKey, claims)
		c.Next()
	}
}

// CurrentUser возвращает claims, сохранённые middleware Required.
// Вызывается только в обработчиках за Required, иначе паникует (ошибка маршрутизации).
func CurrentUser(c *gin.Context) *Claims {
	claims, _ := c.MustGet(claimsKey).(*Claims)
	return claims
}

// UserFromContext — то же без паники, для middleware, работающих на всех маршрутах.
func UserFromContext(c *gin.Context) (*Claims, bool) {
	v, ok := c.Get(claimsKey)
	if !ok {
		return nil, false
	}
	claims, ok := v.(*Claims)
	return claims, ok
}

func abort(c *gin.Context, code, msg string) {
	c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": gin.H{"code": code, "message": msg}})
}
