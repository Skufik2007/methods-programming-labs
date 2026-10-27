package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"lab10/internal/auth"
	"lab10/internal/orders"
)

type handlers struct{ Deps }

// ErrorBody — единый формат ошибок API.
type ErrorBody struct {
	Code    string              `json:"code"`
	Message string              `json:"message"`
	Details []orders.FieldError `json:"details,omitempty"`
}

func fail(c *gin.Context, status int, code, msg string) {
	c.AbortWithStatusJSON(status, gin.H{"error": ErrorBody{Code: code, Message: msg}})
}

// bindJSON разбирает тело и при ошибке сам отвечает клиентом:
// 400 — синтаксис/типы/лишние поля, 413 — слишком большое тело, 422 — валидация.
func bindJSON(c *gin.Context, dst any) bool {
	err := c.ShouldBindJSON(dst)
	if err == nil {
		return true
	}
	if details := orders.Describe(err); details != nil {
		c.AbortWithStatusJSON(http.StatusUnprocessableEntity, gin.H{"error": ErrorBody{
			Code: "validation_failed", Message: "данные не прошли проверку", Details: details,
		}})
		return false
	}

	var (
		syntaxErr *json.SyntaxError
		typeErr   *json.UnmarshalTypeError
		sizeErr   *http.MaxBytesError
	)
	switch {
	case errors.As(err, &sizeErr):
		fail(c, http.StatusRequestEntityTooLarge, "body_too_large", fmt.Sprintf("тело больше %d байт", sizeErr.Limit))
	case errors.Is(err, io.EOF):
		fail(c, http.StatusBadRequest, "empty_body", "пустое тело запроса")
	case errors.Is(err, io.ErrUnexpectedEOF):
		fail(c, http.StatusBadRequest, "invalid_json", "JSON обрывается раньше времени")
	case errors.As(err, &syntaxErr):
		fail(c, http.StatusBadRequest, "invalid_json", fmt.Sprintf("синтаксическая ошибка JSON на позиции %d", syntaxErr.Offset))
	case errors.As(err, &typeErr):
		fail(c, http.StatusBadRequest, "invalid_type", fmt.Sprintf("поле %s: ожидается %s", typeErr.Field, typeErr.Type))
	// У ошибки DisallowUnknownFields в encoding/json нет отдельного типа, поэтому её
	// приходится узнавать по тексту. Формат текста закреплён тестом TestBadRequests.
	case strings.HasPrefix(err.Error(), "json: unknown field"):
		fail(c, http.StatusBadRequest, "unknown_field", "неизвестное поле "+strings.TrimPrefix(err.Error(), "json: unknown field "))
	default:
		fail(c, http.StatusBadRequest, "bad_request", err.Error())
	}
	return false
}

func (h *handlers) ping(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"message": "pong"})
}

func (h *handlers) live(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

func (h *handlers) ready(c *gin.Context) {
	if h.Ready != nil && !h.Ready.Load() {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "shutting_down"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ready"})
}

func (h *handlers) jwks(c *gin.Context) {
	c.Header("Cache-Control", "public, max-age=300")
	c.JSON(http.StatusOK, h.Issuer.JWKS())
}

type loginRequest struct {
	Username string `json:"username" binding:"required,min=3,max=32,alphanum"`
	Password string `json:"password" binding:"required,min=8,max=72"` // 72 — предел bcrypt
}

type TokenResponse struct {
	AccessToken string    `json:"access_token"`
	TokenType   string    `json:"token_type"`
	ExpiresIn   int       `json:"expires_in"`
	ExpiresAt   time.Time `json:"expires_at"`
}

func (h *handlers) login(c *gin.Context) {
	var req loginRequest
	if !bindJSON(c, &req) {
		return
	}
	limitKey := strings.ToLower(req.Username) + "|" + c.ClientIP()
	if ok, retry := h.Limiter.Allow(limitKey); !ok {
		secs := int(math.Ceil(retry.Seconds()))
		c.Header("Retry-After", strconv.Itoa(secs))
		fail(c, http.StatusTooManyRequests, "too_many_attempts",
			fmt.Sprintf("слишком много неудачных попыток входа, повторите через %d с", secs))
		return
	}
	role, err := h.Users.Authenticate(req.Username, req.Password)
	if err != nil {
		h.Limiter.Failure(limitKey)
		fail(c, http.StatusUnauthorized, "invalid_credentials", err.Error())
		return
	}
	h.Limiter.Success(limitKey)
	token, exp, err := h.Issuer.Issue(req.Username, role)
	if err != nil {
		fail(c, http.StatusInternalServerError, "internal", "не удалось выпустить токен")
		return
	}
	c.JSON(http.StatusOK, TokenResponse{
		AccessToken: token, TokenType: "Bearer",
		ExpiresIn: int(time.Until(exp).Seconds()), ExpiresAt: exp.UTC(),
	})
}

func (h *handlers) me(c *gin.Context) {
	cl := auth.CurrentUser(c)
	c.JSON(http.StatusOK, gin.H{"username": cl.Subject, "role": cl.Role, "expires_at": cl.ExpiresAt.Time.UTC()})
}

// validateOrder только проверяет заказ, не сохраняя его. Эндпоинт публичный —
// на нём сравнивается скорость валидации Gin и FastAPI (см. bench/).
func (h *handlers) validateOrder(c *gin.Context) {
	var req orders.CreateRequest
	if !bindJSON(c, &req) {
		return
	}
	cents := orders.TotalCents(req.Items)
	c.JSON(http.StatusOK, gin.H{"valid": true, "items": len(req.Items), "total_cents": cents, "total": float64(cents) / 100})
}

func (h *handlers) createOrder(c *gin.Context) {
	var req orders.CreateRequest
	if !bindJSON(c, &req) {
		return
	}
	o := h.Store.Create(auth.CurrentUser(c).Subject, req)
	c.Header("Location", "/api/v1/orders/"+o.ID)
	c.JSON(http.StatusCreated, o)
}

func (h *handlers) listOrders(c *gin.Context) {
	list := h.Store.List(auth.CurrentUser(c).Subject)
	c.JSON(http.StatusOK, gin.H{"orders": list, "count": len(list)})
}

func (h *handlers) getOrder(c *gin.Context) {
	id := c.Param("id")
	if _, err := uuid.Parse(id); err != nil {
		fail(c, http.StatusBadRequest, "invalid_id", "id должен быть UUID")
		return
	}
	o, err := h.Store.Get(auth.CurrentUser(c).Subject, id)
	switch {
	case errors.Is(err, orders.ErrNotFound), errors.Is(err, orders.ErrForbidden):
		// Чужой заказ отвечает так же, как несуществующий: 403 подтвердил бы,
		// что заказ с таким id существует у другого пользователя.
		fail(c, http.StatusNotFound, "not_found", orders.ErrNotFound.Error())
	default:
		c.JSON(http.StatusOK, o)
	}
}
