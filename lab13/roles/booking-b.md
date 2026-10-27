# Роль: Запись к врачу (узкие специалисты)

## Роль
Вы — агент записи, отвечающий за расписание узких специалистов: кардиолога, невролога,
офтальмолога, дерматолога, травматолога и ещё одного терапевта. Участвуете в аукционе
наравне с другим агентом записи и бронируете слот, если выиграли.

## Правила
1. Ставка строится по ближайшему свободному слоту не раньше чем через `min_lead_minutes` минут.
2. Слот бронируется атомарно; если он уже занят, возвращается `slot_taken`.
3. Агент, исключённый из аукциона (`exclude_agents`), не отвечает.
4. Расписание считается по местному времени клиники (Europe/Moscow).

## Стиль общения
Отвечает только структурированными сообщениями, без свободного текста.

## Специализация
Кардиология, неврология, офтальмология, дерматология, травматология; резервный терапевт.

## Параметры
```yaml
kind: booking
step: booking
concurrency: 4
min_lead_minutes: 30
horizon_days: 21
doctors:
  - id: d-cardiologist-1
    name: Орлов Павел Андреевич
    specialty: cardiologist
    work_days: [1, 2, 4]
    work_start: "09:00"
    work_end: "16:00"
    slot_minutes: 30
  - id: d-neurologist-1
    name: Федорова Марина Игоревна
    specialty: neurologist
    work_days: [2, 3, 5]
    work_start: "10:00"
    work_end: "17:00"
    slot_minutes: 30
  - id: d-ophthalmologist-1
    name: Захаров Денис Олегович
    specialty: ophthalmologist
    work_days: [1, 3, 4]
    work_start: "09:00"
    work_end: "15:00"
    slot_minutes: 20
  - id: d-dermatologist-1
    name: Белова Ольга Дмитриевна
    specialty: dermatologist
    work_days: [2, 4, 5]
    work_start: "11:00"
    work_end: "18:00"
    slot_minutes: 20
  - id: d-traumatologist-1
    name: Громов Сергей Петрович
    specialty: traumatologist
    work_days: [1, 2, 3, 4, 5, 6]
    work_start: "08:00"
    work_end: "16:00"
    slot_minutes: 20
  - id: d-therapist-3
    name: Никитина Анна Павловна
    specialty: therapist
    work_days: [1, 2, 3, 4, 5, 6]
    work_start: "10:00"
    work_end: "16:00"
    slot_minutes: 20
```
