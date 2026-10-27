**Вопрос:** Как включить встроенное в FastAPI ограничение частоты запросов (rate limit) для эндпоинта POST /api/auth/login, чтобы не больше 5 попыток в минуту с одного IP? Приведи код с параметром декоратора роутера.

---

Чтобы включить ограничение частоты запросов для эндпоинта `/api/auth/login` в FastAPI, вы можете использовать библиотеку `fastapi-ratelimit`. Сначала вам нужно установить эту библиотеку:

```bash
pip install fastapi-ratelimit
```

Затем вы можете использовать декоратор `@ratelimit` для вашего эндпоинта. Вот пример кода с параметром декоратора роутера:

```python
from fastapi import FastAPI
from fastapi.ratelimit import RateLimiter
from fastapi.responses import JSONResponse

app = FastAPI()

# Установите количество запросов и интервал в секундах
rate_limit = RateLimiter(limit=5, period=60)


@app.post("/api/auth/login")
@rate_limit
async def login(username: str, password: str):
    # Ваш код для проверки логина и пароля
    if username == "admin" and password == "password":
        return JSONResponse(content={"message": "Login successful"})
    else:
        return JSONResponse(content={"message": "Invalid credentials"}, status_code=401)
```

В этом примере, ограничение частоты запросов установлено на 5 запросов в минуту (`limit=5` и `period=60`). Если пользователь попытается выполнить больше 5 запросов в течение одной минуты, он будет блокирован на 1 минуту.

Обратите внимание, что `fastapi-ratelimit` использует Redis для хранения состояния ограничений. Убедитесь, что Redis доступен и настроен для вашего приложения.
