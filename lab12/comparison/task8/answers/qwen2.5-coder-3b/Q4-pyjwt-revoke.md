**Вопрос:** Как с помощью функции jwt.revoke_token() из PyJWT отозвать токен пользователя после смены пароля? Покажи код для FastAPI.

---

Для отзыва токена пользователя после смены пароля в FastAPI можно использовать функцию `jwt.revoke_token()` из библиотеки `PyJWT`. В этом примере я покажу, как это можно сделать в FastAPI приложении.

1. Убедитесь, что у вас установлены необходимые библиотеки:
   ```bash
   pip install fastapi uvicorn pyjwt
   ```

2. Создайте файл `main.py` с следующим содержимым:

```python
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional

app = FastAPI()

# Настройки JWT
SECRET_KEY = "your-secret-key"  # Замените на ваш собственный ключ
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Время жизни токена
ACCESS_TOKEN_EXPIRE_MINUTES = 30


# Функция для генерации токена
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# Функция для проверки токена
def verify_access_token(token: str, credentials_exception):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    return token_data


# Функция для отзыва токена
def revoke_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        # Здесь можно добавить логику для отзыва токена в базе данных
        print(f"Token revoked for user: {username}")
    except JWTError:
        raise credentials_exception


# Модель данных для токена
class TokenData:
    username: Optional[str] = None


# Модель данных для входа
class UserInDB(BaseModel):
    username: str
    hashed_password: str


# Модель данных для выхода
class Token(BaseModel):
    access_token: str
    token_type: str


# Данные для теста
users_db = {"johndoe": {"hashed_password": "hashed_password_for_johndoe"}}


# Функция для проверки пароля
def authenticate_user(username: str, password: str):
    user = users_db.get(username)
    if not user:
        return False
    if not verify_password(password, user["hashed_password"]):
        return False
    return user


# Функция для проверки пароля
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


# Функция для получения токена
@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)
    return {"access_token": access_token, "token_type": "bearer"}


# Функция для отзыва токена
@app.post("/revoke_token")
async def revoke_token(token: str = Depends(oauth2_scheme)):
    revoke_token(token)
    return {"message": "Token revoked"}


# Пример использования
@app.post("/change_password")
async def change_password(username: str, old_password: str, new_password: str):
    user = authenticate_user(username, old_password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Здесь можно обновить пароль в базе данных
    print(f"Password changed for user: {username}")
    # Отзываем токен пользователя
    token = create_access_token(data={"sub": username})
    revoke_token(token)
    return {"message": "Password changed and token revoked"}
```

### Объяснение кода:

1. **Настройки JWT**: Устанавливаем секретный ключ и алгоритм для создания токена.

2. **Функции для JWT**: 
   - `create_access_token`: Создает новый токен с указанным временем жизни.
   - `verify_access_token`: Проверяет токен и возвращает данные пользователя.
   - `revoke_token`: Отзывает токен пользователя (в данном случае просто выводит сообщение в консоль).

3. **Модели данных**:
   - `TokenData`: Модель данных для токена.
   - `UserInDB`: Модель данных для пользователя в базе данных.
   - `Token`: Модель данных для ответа с токеном.

4. **Функции для аутентификации**:
   - `authenticate_user`: Проверяет пароль пользователя.
   - `verify_password`: Проверяет пароль с использованием библиотеки `passlib`.

5. **Функции для API**:
   - `/token`: Получение токена по логину и паролю.
   - `/revoke_token`: Отзыв токена.
   - `/change_password`: Изменение пароля и отзыв токена пользователя.

### Пример использования:

- Вызов `/token` для получения токена.
- Вызов `/change_password` для изменения пароля и отзыва токена пользователя.
- Вызов `/revoke_token` для отзыва токена.

### Замечания:

- В данном примере токен отзывается только в консоль. В реальном приложении вам нужно добавить логику для отзыва токена в базе данных.
- Для безопасности рекомендуется использовать более надежный алгоритм и более длинный секретный ключ.
- Для хранения паролей рекомендуется использовать библиотеку `passlib` с алгоритмом хеширования, такой как `bcrypt`.
