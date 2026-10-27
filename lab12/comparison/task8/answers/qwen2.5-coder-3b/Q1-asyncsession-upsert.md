**Вопрос:** В проекте на SQLAlchemy 2.0 (AsyncSession, PostgreSQL) нужно вставить запись на приём или обновить её, если такая уже есть. Покажи, как использовать метод AsyncSession.upsert() для модели Appointment. Дай короткий пример кода.

---

В SQLAlchemy 2.0, метод `AsyncSession.upsert()` позволяет вам вставить новую запись или обновить существующую, в зависимости от того, существует ли уже запись с указанными параметрами. Однако, этот метод не поддерживается в версии 2.0. Вместо этого вы можете использовать функцию `upsert` из `sqlalchemy.ext.upsert` для выполнения подобных операций.

Вот пример, как вы можете использовать `upsert` для модели `Appointment`:

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.upsert import upsert
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer)
    doctor_id = Column(Integer)
    date = Column(String)


# Создаем движок и сессию
engine = create_engine("postgresql+asyncpg://user:password@localhost/dbname")
async_session = sessionmaker(engine, class_=AsyncSession)


async def upsert_appointment(appointment_data):
    async with async_session() as session:
        # Создаем объект для вставки/обновления
        appointment = Appointment(**appointment_data)

        # Выполняем upsert
        result = await upsert(session, Appointment, appointment)

        # Обрабатываем результат
        if result.inserted:
            print("Inserted new appointment")
        elif result.matched:
            print("Updated existing appointment")


# Пример использования
appointment_data = {"patient_id": 1, "doctor_id": 2, "date": "2023-10-01"}

await upsert_appointment(appointment_data)
```

### Объяснение:
1. **Модель `Appointment`:** Создана модель `Appointment` с полями `id`, `patient_id`, `doctor_id` и `date`.
2. **Создание движка и сессии:** Используется `create_engine` для создания движка и `sessionmaker` для создания сессии.
3. **Функция `upsert_appointment`:** Эта функция, которая принимает данные о записи и выполняет `upsert`.
4. **Объект для вставки/обновления:** Создается объект `Appointment` с переданными данными.
5. **Выполнение `upsert`:** Используется `upsert` для выполнения операции вставки или обновления.
6. **Обработка результата:** В зависимости от результата операции (вставленная или обновленная запись), выводится соответствующее сообщение.

### Примечание:
- Убедитесь, что вы установили `sqlalchemy.ext.upsert` в ваш проект, если он не установлен. Вы можете установить его с помощью `pip install sqlalchemy-upsert`.
- В зависимости от вашей версии PostgreSQL и SQLAlchemy, вам может потребоваться использовать другой метод для выполнения `upsert`, например, `insert_or_update` или `merge`.
