/* Система управления клиникой: клиентская часть (ES2020, без сборки и библиотек).
 *
 * Правила безопасности:
 *  - данные из API попадают в DOM только через textContent / createElement / append(строка);
 *    innerHTML, outerHTML, insertAdjacentHTML и document.write не используются;
 *  - токен хранится в sessionStorage; при ответе 401 выполняется выход на экран входа.
 *
 * Структура файла:
 *  1. константы и состояние          6. вход / регистрация / выход
 *  2. DOM-помощники                  7. разделы пациента
 *  3. API-клиент и ошибки            8. разделы врача
 *  4. даты и время (МСК)             9. разделы регистратора
 *  5. общие компоненты UI           10. разделы администратора, SVG-графики
 */
(() => {
  'use strict';

  /* ======================================================================
   * 1. Константы и состояние
   * ==================================================================== */

  const TZ = 'Europe/Moscow'; // часовой пояс клиники
  const TOKEN_KEY = 'clinic_token';
  const PAGE_SIZE = 100; // максимальный limit, который принимает API

  const ROLE_LABELS = {
    admin: 'Администратор',
    registrar: 'Регистратор',
    doctor: 'Врач',
    patient: 'Пациент',
  };
  const STATUS_LABELS = {
    scheduled: 'Запланирована',
    completed: 'Состоялась',
    cancelled: 'Отменена',
    no_show: 'Неявка',
  };
  const DAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']; // ISO: 1 = понедельник

  // Разделы (id секции = "view-" + ключ) и их подписи в меню.
  const NAV_LABELS = {
    'p-appointments': 'Мои записи',
    'p-new': 'Новая запись',
    'p-records': 'Медкарта и рецепты',
    'p-profile': 'Профиль',
    'd-schedule': 'Расписание',
    'd-patients': 'Медкарты пациентов',
    'r-appointments': 'Записи',
    'r-book': 'Записать пациента',
    'r-patients': 'Пациенты',
    'a-dashboard': 'Аналитика',
    'a-users': 'Пользователи',
    'a-doctors': 'Врачи и графики',
  };
  const ROLE_NAV = {
    patient: ['p-appointments', 'p-new', 'p-records', 'p-profile'],
    doctor: ['d-schedule', 'd-patients'],
    registrar: ['r-appointments', 'r-book', 'r-patients'],
    admin: ['a-dashboard', 'a-users', 'a-doctors', 'r-appointments', 'r-book', 'r-patients'],
  };

  // Всё, что запоминается между переходами; сбрасывается при выходе.
  const state = {
    token: null,
    user: null,
    patient: null, // собственный профиль пациента
    doctors: null, // список врачей (GET /api/doctors)
    doctorMap: new Map(),
    patientMap: new Map(),
    pendingPatientId: null, // пациент, выбранный в расписании для просмотра медкарты
    editDoctor: null, // редактируемый профиль врача (null = режим создания)
    doctorUsers: [], // пользователи с ролью doctor (для формы профиля)
  };
  let memoryToken = null; // запасной вариант, если sessionStorage недоступен

  /* ======================================================================
   * 2. DOM-помощники
   * ==================================================================== */

  const $ = (id) => document.getElementById(id);
  let uidCounter = 0;
  const uid = () => `u${++uidCounter}`;
  const PROP_ATTRS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected']);

  /** Создаёт элемент. Строки-потомки превращаются в текстовые узлы (никакого HTML). */
  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'on') {
        for (const [eventName, handler] of Object.entries(value)) node.addEventListener(eventName, handler);
      } else if (PROP_ATTRS.has(key)) node[key] = value;
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    node.append(...children.flat().filter((c) => c != null && c !== false));
    return node;
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';

  /** Создаёт SVG-элемент; атрибут text задаёт текстовое содержимое. */
  function svg(tag, attrs = {}, ...children) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null) continue;
      if (key === 'text') node.textContent = value;
      else node.setAttribute(key, String(value));
    }
    node.append(...children);
    return node;
  }

  /** Кнопка-действие (type="button"). */
  function btn(text, onClick, variant = 'btn-secondary') {
    return h('button', { type: 'button', class: `btn ${variant}`.trim(), text, on: { click: onClick } });
  }

  const unique = (items) => [...new Set(items)];

  /* ======================================================================
   * 3. API-клиент и ошибки
   * ==================================================================== */

  /** Ошибка ответа API: status = 0 при сетевой ошибке, detail — строка или список ошибок валидации. */
  class ApiError extends Error {
    constructor(status, detail) {
      super(typeof detail === 'string' ? detail : `HTTP ${status}`);
      this.status = status;
      this.detail = detail;
    }
  }
  /** Ошибка, сформированная самим интерфейсом (текст уже на русском). */
  class UiError extends Error {}

  function storageGet() {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return memoryToken;
    }
  }
  function storageSet(token) {
    memoryToken = token;
    try {
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* sessionStorage недоступен: остаёмся на значении в памяти */
    }
  }

  /**
   * Запрос к API. query: пустые значения отбрасываются; body отправляется как JSON.
   * auth: false — не слать токен и не трактовать 401 как «сессия истекла» (форма входа).
   */
  async function api(path, { method = 'GET', query, body, auth = true } = {}) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(query || {})) {
      if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
    }
    const headers = { Accept: 'application/json' };
    if (auth && state.token) headers.Authorization = `Bearer ${state.token}`;
    const init = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(url, init);
    } catch {
      throw new ApiError(0, null);
    }
    let data = null;
    if (response.status !== 204) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    }
    if (!response.ok) {
      if (response.status === 401 && auth) handleUnauthorized();
      throw new ApiError(response.status, data && data.detail !== undefined ? data.detail : null);
    }
    return data;
  }

  /** Загружает все страницы списка (по 100), но не больше maxItems элементов. */
  async function fetchAll(path, query = {}, maxItems = 500) {
    const result = [];
    while (result.length < maxItems) {
      const page = await api(path, { query: { ...query, limit: PAGE_SIZE, offset: result.length } });
      result.push(...page);
      if (page.length < PAGE_SIZE) break;
    }
    return result;
  }

  // --- перевод сообщений сервера и ошибок валидации -----------------------

  const FIELD_LABELS = {
    email: 'Email', password: 'Пароль', full_name: 'ФИО', birth_date: 'Дата рождения',
    phone: 'Телефон', policy_number: 'Номер полиса', diagnosis_code: 'Код МКБ-10',
    notes: 'Заметки', medication: 'Препарат', dosage: 'Дозировка', instructions: 'Указания',
    reason: 'Причина', specialty: 'Специальность', cabinet: 'Кабинет',
    slot_minutes: 'Длительность приёма', work_start: 'Начало смены', work_end: 'Конец смены',
    work_days: 'Рабочие дни', role: 'Роль', user_id: 'Пользователь', starts_at: 'Начало',
    ends_at: 'Окончание', date_from: 'Дата «с»', date_to: 'Дата «по»', date: 'Дата', q: 'Поиск',
  };
  // [шаблон, замена]: первое совпавшее правило заменяет весь текст.
  const MESSAGE_RULES = [
    [/^Incorrect email or password$/, 'Неверный email или пароль'],
    [/^(Not authenticated|Invalid or expired token|Token is no longer valid)$/, 'Сессия истекла. Войдите снова'],
    [/^Insufficient permissions.*$/, 'Недостаточно прав для этого действия'],
    [/^Medical data is not available.*$/, 'Медицинские данные недоступны административному персоналу'],
    [/^Email is already registered$/, 'Этот email уже зарегистрирован'],
    [/^Email or policy number is already registered$/, 'Этот email или номер полиса уже зарегистрирован'],
    [/^A patient with this policy number already exists.*$/, 'Пациент с таким номером полиса уже есть. Обратитесь к регистратору клиники, чтобы привязать аккаунт'],
    [/^The doctor already has an appointment at this time$/, 'У врача уже есть запись на это время'],
    [/^The patient already has an appointment at this time$/, 'У пациента уже есть запись на это время'],
    [/^The doctor is not available for new appointments$/, 'Врач сейчас не принимает новые записи'],
    [/^Appointment not found$/, 'Запись не найдена'],
    [/^Doctor not found$/, 'Врач не найден'],
    [/^Patient not found$/, 'Пациент не найден'],
    [/^Medical record not found$/, 'Запись медкарты не найдена'],
    [/^The appointment has not started yet$/, 'Приём ещё не начался: отметить результат можно после его начала'],
    [/^Only scheduled appointments can be modified$/, 'Изменять можно только запланированные записи'],
    [/^Patients can only modify or cancel.*$/, 'Пациент может изменить или отменить запись не позже чем за 2 часа до начала'],
    [/^Patients can only book appointments for their own profile$/, 'Пациент может записывать только себя'],
    [/^Cannot change status from.*$/, 'Для этой записи такой переход статуса недопустим'],
    [/^Prescriptions can only be issued for completed appointments$/, 'Рецепт можно выписать только после состоявшегося приёма'],
    [/^Medical records can only be linked to completed appointments$/, 'Диагноз можно добавить только к состоявшемуся приёму'],
    [/^date_from must not be later than date_to$/, 'Дата «с» не может быть позже даты «по»'],
    [/^Value error, /, ''],
    [/^password must be at least (\d+) characters long$/, 'пароль не короче $1 символов'],
    [/^password must not exceed (\d+) bytes.*$/, 'пароль слишком длинный (не более $1 байт)'],
    [/^birth_date must not be in the future$/, 'дата рождения не может быть в будущем'],
    [/^birth_date is implausibly far in the past$/, 'нереальная дата рождения'],
    [/^work_start must be earlier than work_end.*$/, 'начало смены должно быть раньше конца'],
    [/^Field required$/, 'обязательное поле'],
    [/^String should have at least (\d+) character.*$/, 'не короче $1 симв.'],
    [/^String should have at most (\d+) character.*$/, 'не длиннее $1 симв.'],
    [/^String should match pattern.*$/, 'неверный формат'],
    [/^Input should be a valid (integer|number).*$/, 'нужно число'],
    [/^Input should be a valid (date|datetime).*$/, 'неверная дата или время'],
    [/^Input should be a valid time.*$/, 'неверное время'],
    [/^Input should be greater than.*$/, 'значение слишком мало'],
    [/^Input should be less than.*$/, 'значение слишком велико'],
    [/^Extra inputs are not permitted$/, 'недопустимое поле'],
  ];
  const HTTP_MESSAGES = {
    400: 'Некорректный запрос',
    403: 'Недостаточно прав для этого действия',
    404: 'Не найдено',
    409: 'Конфликт с текущим состоянием данных',
    422: 'Данные не прошли проверку',
    429: 'Слишком много запросов, повторите позже',
  };

  /** Переводит известные тексты сервера; неизвестные возвращает как есть. */
  function translateMessage(text) {
    for (const [pattern, replacement] of MESSAGE_RULES) {
      if (pattern.test(text)) {
        const result = text.replace(pattern, replacement);
        // Правило-префикс («Value error, ») оставляет хвост — переводим и его.
        return replacement === '' ? translateMessage(result) : result;
      }
    }
    return text;
  }

  /** Одна строка ошибки валидации FastAPI/Pydantic: «Поле: сообщение». */
  function validationLine(item) {
    const loc = Array.isArray(item.loc) ? item.loc.filter((p) => !['body', 'query', 'path'].includes(p)) : [];
    const field = loc.map((p) => FIELD_LABELS[p] || String(p)).join(' / ');
    const message = translateMessage(String(item.msg || 'ошибка'));
    return field ? `${field}: ${message}` : message;
  }

  /** Человекочитаемый текст любой ошибки (detail — строка или список). */
  function errorText(error) {
    if (error instanceof UiError) return error.message;
    if (!(error instanceof ApiError)) return 'Непредвиденная ошибка. Обновите страницу и повторите попытку.';
    if (error.status === 0) return 'Нет связи с сервером. Проверьте подключение и повторите попытку.';
    if (Array.isArray(error.detail)) return error.detail.map(validationLine).join('; ');
    if (typeof error.detail === 'string') return translateMessage(error.detail);
    return HTTP_MESSAGES[error.status] || `Ошибка сервера (код ${error.status})`;
  }

  function showError(node, error) {
    node.textContent = errorText(error);
    node.hidden = false;
  }
  function clearError(node) {
    node.textContent = '';
    node.hidden = true;
  }
  /** Блок ошибки формы — первый .form-error внутри неё. */
  const errorNode = (form) => form.querySelector('.form-error');

  /**
   * Выполняет действие: блокирует кнопку на время запроса (защита от двойной отправки),
   * очищает прежнюю ошибку и показывает новую рядом с формой/карточкой.
   */
  async function run(button, errNode, task) {
    if (errNode) clearError(errNode);
    if (button) button.disabled = true;
    try {
      return await task();
    } catch (error) {
      if (!(error instanceof ApiError || error instanceof UiError)) console.error(error); // ошибка в коде, а не ответ сервера
      if (errNode) showError(errNode, error);
      return undefined;
    } finally {
      if (button) button.disabled = false;
    }
  }

  let toastTimer = 0;
  /** Короткое уведомление об успехе (aria-live). */
  function notify(message) {
    const toast = $('toast');
    toast.textContent = message;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.textContent = ''; }, 5000);
  }

  /* ======================================================================
   * 4. Даты и время (часовой пояс клиники)
   * ==================================================================== */

  const dateTimeFmt = new Intl.DateTimeFormat('ru-RU', {
    timeZone: TZ, day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const timeFmt = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });
  const ymdFmt = new Intl.DateTimeFormat('en-US', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  const numberFmt = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });

  /** «дд.мм.гггг, чч:мм» по Москве. */
  const formatDateTime = (iso) => dateTimeFmt.format(new Date(iso));
  const formatTime = (iso) => timeFmt.format(new Date(iso));

  /** Местная дата клиники YYYY-MM-DD для момента времени (Date или ISO-строка). */
  function localYmd(value) {
    const parts = {};
    for (const p of ymdFmt.formatToParts(new Date(value))) parts[p.type] = p.value;
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  const todayYmd = () => localYmd(new Date());

  /** Прибавляет дни к строке YYYY-MM-DD (арифметика по UTC, без влияния пояса браузера). */
  function addDays(ymd, days) {
    const [y, m, d] = ymd.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
  }

  /** «гггг-мм-дд» -> «дд.мм.гггг» (дата без времени не пересчитывается по поясам). */
  function formatDate(ymd) {
    const [y, m, d] = String(ymd).split('-');
    return `${d}.${m}.${y}`;
  }
  /** Момент времени -> ISO 8601 с часовым поясом (UTC, суффикс Z) для отправки на сервер. */
  const toIso = (value) => new Date(value).toISOString();

  /* ======================================================================
   * 5. Общие компоненты UI
   * ==================================================================== */

  const statusBadge = (status) => h('span', { class: `badge badge-${status}`, text: STATUS_LABELS[status] || status });

  /** Список «ключ — значение» (dl); пустые значения пропускаются. */
  function kvList(pairs) {
    const dl = h('dl', { class: 'kv' });
    for (const [key, value] of pairs) {
      if (value == null || value === '') continue;
      dl.append(h('dt', { text: key }), h('dd', { text: String(value) }));
    }
    return dl;
  }

  /** Карточка: заголовок, значок, пары «ключ — значение», кнопки, блок ошибки и блок для вложенного. */
  function card(heading, badge, pairs, actions = []) {
    return h('li', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', { class: 'card-title', text: heading }), badge),
      kvList(pairs),
      actions.length ? h('div', { class: 'actions' }, ...actions) : null,
      h('p', { class: 'form-error card-error', role: 'alert', hidden: true }),
      h('div', { class: 'extras' }));
  }
  const cardError = (el) => el.closest('.card').querySelector('.card-error');
  const cardExtras = (el) => el.closest('.card').querySelector('.extras');

  /** Обёртка обработчика кнопки в карточке: блокировка + вывод ошибки в карточку. */
  function act(task) {
    return (event) => {
      const button = event.currentTarget;
      return run(button, cardError(button), () => task(button));
    };
  }

  const loadingItem = () => h('li', { class: 'empty', text: 'Загрузка…' });

  /** Заполняет список карточками или сообщением «пусто». */
  function fillList(list, items, render, emptyText) {
    list.replaceChildren(...(items.length ? items.map(render) : [h('li', { class: 'empty', text: emptyText })]));
  }

  /** Показывает панель в extras карточки; повторный вызов с тем же ключом закрывает её. */
  function togglePanel(extras, key, build) {
    const existing = extras.querySelector(`[data-panel="${key}"]`);
    if (existing) {
      existing.remove();
      return;
    }
    const panel = build();
    panel.dataset.panel = key;
    extras.append(panel);
  }

  /**
   * Постраничный список с кнопкой «Показать ещё».
   * fetchPage(offset, limit) -> массив; render(item) -> <li>.
   */
  function createPagedList({ list, more, errorBox, fetchPage, render, empty, pageSize = 20 }) {
    let offset = 0;
    let version = 0; // отбрасывает устаревшие ответы при быстрой смене фильтров
    async function load(reset) {
      const mine = ++version;
      if (reset) {
        offset = 0;
        list.replaceChildren(loadingItem());
        more.hidden = true;
      }
      clearError(errorBox);
      more.disabled = true;
      try {
        const items = await fetchPage(offset, pageSize);
        if (mine !== version) return;
        if (reset) list.replaceChildren();
        if (reset && !items.length) list.append(h('li', { class: 'empty', text: empty }));
        for (const item of items) list.append(render(item));
        offset += items.length;
        more.hidden = items.length < pageSize;
      } catch (error) {
        if (mine !== version) return;
        if (reset) list.replaceChildren();
        showError(errorBox, error);
      } finally {
        more.disabled = false;
      }
    }
    more.addEventListener('click', () => load(false));
    return { reset: () => load(true) };
  }

  /**
   * Форма в карточке (диагноз, рецепт). fields: [{name, label, textarea, required, maxlength, pattern, hint}].
   * onSubmit(values, form) вызывается с обрезанными по краям значениями.
   */
  function inlineForm({ title, fields, submitText, onSubmit }) {
    const form = h('form', { class: 'inline-form' });
    const grid = h('div', { class: 'form-grid' });
    for (const f of fields) {
      const id = uid();
      const hintId = f.hint ? `${id}-hint` : null;
      const common = { id, name: f.name, required: f.required, maxlength: f.maxlength, 'aria-describedby': hintId };
      const control = f.textarea
        ? h('textarea', { ...common, rows: 3 })
        : h('input', { ...common, type: 'text', pattern: f.pattern, autocomplete: 'off' });
      grid.append(h('div', { class: 'field' },
        h('label', { for: id, text: f.label }),
        control,
        f.hint ? h('small', { id: hintId, text: f.hint }) : null));
    }
    const err = h('p', { class: 'form-error', role: 'alert', hidden: true });
    form.append(h('h4', { text: title }), grid, err,
      h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn', text: submitText })));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const values = {};
      for (const f of fields) values[f.name] = form.elements[f.name].value.trim();
      return run(event.submitter, err, () => onSubmit(values, form));
    });
    return form;
  }

  // --- кэши справочных данных ----------------------------------------------

  /** Список врачей (кэшируется; force = обновить). */
  async function ensureDoctors(force = false) {
    if (state.doctors && !force) return state.doctors;
    state.doctors = await api('/api/doctors');
    for (const doctor of state.doctors) state.doctorMap.set(doctor.id, doctor);
    return state.doctors;
  }
  /** Врач по id: из кэша, иначе отдельным запросом (деактивированных нет в общем списке); null при ошибке. */
  async function getDoctor(id) {
    if (state.doctorMap.has(id)) return state.doctorMap.get(id);
    try {
      const doctor = await api(`/api/doctors/${id}`);
      state.doctorMap.set(id, doctor);
      return doctor;
    } catch {
      return null;
    }
  }
  async function getPatient(id) {
    if (state.patientMap.has(id)) return state.patientMap.get(id);
    try {
      const patient = await api(`/api/patients/${id}`);
      state.patientMap.set(id, patient);
      return patient;
    } catch {
      return null;
    }
  }
  async function getMyPatient() {
    if (!state.patient) state.patient = await api('/api/patients/me');
    return state.patient;
  }
  /** Профиль врача текущего пользователя (по user_id) или null. */
  async function getMyDoctor() {
    const doctors = await ensureDoctors();
    return doctors.find((d) => d.user_id === state.user.id) || null;
  }
  const doctorName = (id) => (state.doctorMap.get(id) ? state.doctorMap.get(id).full_name : `Врач №${id}`);
  const patientName = (id) => (state.patientMap.get(id) ? state.patientMap.get(id).full_name : `Пациент №${id}`);

  /* ---- выбор свободного слота ------------------------------------------- */

  let slotRequestCounter = 0;

  /**
   * Показывает в box свободные слоты врача на дату (радиокнопки). onPick(slot|null) —
   * выбранный слот или null (при загрузке/ошибке/смене даты).
   */
  async function renderSlots(box, doctorId, date, onPick) {
    const mine = ++slotRequestCounter;
    box.dataset.request = String(mine);
    onPick(null);
    box.replaceChildren(h('p', { class: 'muted', text: 'Загрузка свободного времени…' }));
    let slots;
    try {
      slots = await api(`/api/doctors/${doctorId}/slots`, { query: { date } });
    } catch (error) {
      if (box.dataset.request !== String(mine)) return;
      box.replaceChildren(h('p', { class: 'form-error', role: 'alert', text: errorText(error) }));
      return;
    }
    if (box.dataset.request !== String(mine)) return; // пришёл ответ на устаревший запрос
    if (!slots.length) {
      box.replaceChildren(h('p', { class: 'muted', text: 'На эту дату нет свободного времени.' }));
      return;
    }
    const group = `slot-${uid()}`;
    const items = slots.map((slot, index) => {
      const id = `${group}-${index}`;
      return h('span', { class: 'slot' },
        h('input', { type: 'radio', name: group, id, on: { change: () => onPick(slot) } }),
        h('label', { for: id, text: formatTime(slot.starts_at) }));
    });
    box.replaceChildren(h('fieldset', { class: 'slots' }, h('legend', { text: 'Свободное время (МСК)' }), ...items));
  }

  /**
   * Подключает логику формы записи (специальность -> врач -> дата -> слот -> отправка).
   * Форма должна содержать поля specialty, doctor, date, reason, блок .slot-box и .form-error.
   * getPatientId() возвращает id пациента; onBooked() вызывается после успешной записи.
   * Возвращает {prepare()} — подготовка списков при открытии раздела.
   */
  function setupBookingForm(form, getPatientId, onBooked) {
    const f = form.elements;
    const slotBox = form.querySelector('.slot-box');
    let picked = null;

    const refreshSlots = () => {
      const doctorId = Number(f.doctor.value);
      if (!doctorId || !f.date.value) {
        picked = null;
        slotBox.replaceChildren(h('p', { class: 'muted', text: 'Выберите врача и дату, чтобы увидеть свободное время.' }));
        return;
      }
      renderSlots(slotBox, doctorId, f.date.value, (slot) => { picked = slot; });
    };
    const fillDoctors = () => {
      const specialty = f.specialty.value;
      const doctors = state.doctors.filter((d) => !specialty || d.specialty === specialty);
      f.doctor.replaceChildren(
        h('option', { value: '', text: '— выберите врача —' }),
        ...doctors.map((d) => h('option', { value: String(d.id), text: `${d.full_name}, каб. ${d.cabinet}` })));
    };

    f.specialty.addEventListener('change', () => { fillDoctors(); refreshSlots(); });
    f.doctor.addEventListener('change', refreshSlots);
    f.date.addEventListener('change', refreshSlots);

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      return run(event.submitter, errorNode(form), async () => {
        const patientId = getPatientId();
        if (!patientId) throw new UiError('Выберите пациента.');
        if (!picked) throw new UiError('Выберите свободное время.');
        const reason = f.reason.value.trim();
        await api('/api/appointments', {
          method: 'POST',
          body: {
            doctor_id: Number(f.doctor.value),
            patient_id: patientId,
            starts_at: toIso(picked.starts_at),
            ends_at: toIso(picked.ends_at),
            reason: reason || undefined,
          },
        });
        notify('Запись создана');
        f.reason.value = '';
        refreshSlots();
        if (onBooked) await onBooked();
      });
    });

    return {
      async prepare() {
        await ensureDoctors();
        const specialties = unique(state.doctors.map((d) => d.specialty)).sort((a, b) => a.localeCompare(b, 'ru'));
        f.specialty.replaceChildren(h('option', { value: '', text: 'Все специальности' }),
          ...specialties.map((s) => h('option', { value: s, text: s })));
        fillDoctors();
        f.date.min = todayYmd();
        if (!f.date.value || f.date.value < f.date.min) f.date.value = f.date.min;
        refreshSlots();
      },
    };
  }

  /* ======================================================================
   * 6. Вход, регистрация, выход, навигация
   * ==================================================================== */

  function clearSession() {
    storageSet(null);
    state.token = null;
    state.user = null;
    state.patient = null;
    state.doctors = null;
    state.doctorMap.clear();
    state.patientMap.clear();
    state.pendingPatientId = null;
  }

  /** Показывает экран входа; message — пояснение (например, «сессия истекла»). */
  function showAuth(message) {
    $('userbox').hidden = true;
    $('nav').hidden = true;
    $('nav-list').replaceChildren();
    for (const section of document.querySelectorAll('.view')) section.hidden = true;
    $('screen-auth').hidden = false;
    $('form-login').reset();
    $('form-register').reset();
    const info = $('auth-info');
    info.textContent = message || '';
    info.hidden = !message;
  }

  /** 401 от защищённого запроса: токен недействителен — выходим на экран входа. */
  function handleUnauthorized() {
    if (!state.token) return;
    clearSession();
    showAuth('Сессия истекла или недействительна. Войдите снова.');
  }

  function logout() {
    clearSession();
    showAuth('');
  }

  /** Сохраняет токен, узнаёт пользователя и строит интерфейс по роли. */
  async function startSession(token) {
    state.token = token;
    storageSet(token);
    try {
      state.user = await api('/api/auth/me'); // при 401 handleUnauthorized уже вернул на экран входа
    } catch (error) {
      if (state.token) clearSession(); // сетевая ошибка и т.п.: не оставляем «полусессию»
      throw error;
    }
    buildApp();
  }

  /** Меню по роли и переход в первый раздел. */
  function buildApp() {
    $('screen-auth').hidden = true;
    $('user-name').textContent = state.user.full_name;
    $('user-role').textContent = ROLE_LABELS[state.user.role] || state.user.role;
    $('userbox').hidden = false;
    const keys = ROLE_NAV[state.user.role] || [];
    $('nav-list').replaceChildren(...keys.map((key) => h('li', {},
      h('button', { type: 'button', 'data-view': key, text: NAV_LABELS[key], on: { click: () => showView(key) } }))));
    $('nav').hidden = false;
    if (keys.length) showView(keys[0]);
  }

  const VIEW_LOADERS = {}; // заполняется в разделах ниже: ключ -> функция загрузки

  /** Переключает раздел без перезагрузки страницы и запускает его загрузчик. */
  function showView(key) {
    for (const section of document.querySelectorAll('.view')) section.hidden = section.id !== `view-${key}`;
    for (const button of $('nav-list').querySelectorAll('button')) {
      if (button.dataset.view === key) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    const heading = $(`view-${key}`).querySelector('h2');
    if (heading) heading.focus();
    if (VIEW_LOADERS[key]) VIEW_LOADERS[key]();
  }

  /** Телефон -> E.164: убирает пробелы и скобки, 8XXXXXXXXXX и 7XXXXXXXXXX -> +7XXXXXXXXXX. */
  function normalizePhone(raw) {
    const value = raw.trim().replace(/[\s()-]/g, '');
    if (!value) return undefined;
    if (/^8\d{10}$/.test(value)) return `+7${value.slice(1)}`;
    if (/^7\d{10}$/.test(value)) return `+${value}`;
    return value;
  }

  async function login(email, password) {
    const token = await api('/api/auth/login', { method: 'POST', auth: false, body: { email, password } });
    await startSession(token.access_token);
  }

  function bindAuth() {
    const loginForm = $('form-login');
    loginForm.addEventListener('submit', (event) => {
      event.preventDefault();
      return run(event.submitter, errorNode(loginForm), async () => {
        await login(loginForm.elements.email.value.trim(), loginForm.elements.password.value);
        loginForm.reset();
      });
    });

    const regForm = $('form-register');
    regForm.addEventListener('submit', (event) => {
      event.preventDefault();
      return run(event.submitter, errorNode(regForm), async () => {
        const f = regForm.elements;
        const email = f.email.value.trim();
        const password = f.password.value;
        await api('/api/auth/register', {
          method: 'POST',
          auth: false,
          body: {
            email,
            password,
            full_name: f.full_name.value.trim(),
            birth_date: f.birth_date.value,
            phone: normalizePhone(f.phone.value),
            policy_number: f.policy_number.value.trim(),
          },
        });
        await login(email, password); // сразу входим под новой учётной записью
      });
    });

    $('btn-logout').addEventListener('click', logout);
  }

  /* ======================================================================
   * 7. Разделы пациента
   * ==================================================================== */

  /** Отмена записи (общая для пациента и регистратора). */
  async function cancelAppointment(appointment, reload) {
    if (!window.confirm(`Отменить запись на ${formatDateTime(appointment.starts_at)}?`)) return;
    await api(`/api/appointments/${appointment.id}/cancel`, { method: 'POST' });
    notify('Запись отменена');
    await reload();
  }

  function patientAppointmentCard(a) {
    const doctor = state.doctorMap.get(a.doctor_id);
    const canCancel = a.status === 'scheduled' && new Date(a.starts_at) > new Date();
    return card(formatDateTime(a.starts_at), statusBadge(a.status), [
      ['Врач', doctorName(a.doctor_id)],
      ['Специальность', doctor && doctor.specialty],
      ['Кабинет', doctor && doctor.cabinet],
      ['Причина', a.reason],
    ], canCancel ? [btn('Отменить запись', act(() => cancelAppointment(a, loadPatientAppointments)), 'btn-danger')] : []);
  }

  async function loadPatientAppointments() {
    const list = $('p-app-list');
    const err = $('p-app-error');
    clearError(err);
    list.replaceChildren(loadingItem());
    try {
      const [appointments] = await Promise.all([
        fetchAll('/api/appointments', { status: $('p-app-filter').value }),
        ensureDoctors(),
      ]);
      await Promise.all(unique(appointments.map((a) => a.doctor_id)).map((id) => getDoctor(id)));
      fillList(list, appointments, patientAppointmentCard, 'Записей нет.');
    } catch (error) {
      list.replaceChildren();
      showError(err, error);
    }
  }
  VIEW_LOADERS['p-appointments'] = loadPatientAppointments;

  const patientBooking = () => setupBookingForm($('form-new'), () => (state.patient ? state.patient.id : null),
    () => { VIEW_LOADERS['p-appointments'](); });
  let newBooking = null;

  async function loadPatientNew() {
    const err = errorNode($('form-new'));
    clearError(err);
    try {
      await getMyPatient();
      await newBooking.prepare();
    } catch (error) {
      showError(err, error);
    }
  }
  VIEW_LOADERS['p-new'] = loadPatientNew;

  /** Запись медкарты (общая для пациента и врача): код МКБ-10 в заголовке, заметки — абзацем. */
  function recordItem(record) {
    const li = card(record.diagnosis_code, null, [
      ['Дата', formatDateTime(record.created_at)],
      ['Врач', doctorName(record.doctor_id)],
    ]);
    if (record.notes) li.querySelector('.extras').append(h('p', { class: 'pre', text: record.notes }));
    return li;
  }

  /** Список записей медкарты с пояснением, если записей нет. */
  function renderRecords(list, records) {
    fillList(list, records, recordItem, 'Записей в медкарте нет.');
  }

  async function loadPatientRecords() {
    const err = $('p-rec-error');
    clearError(err);
    $('p-rec-list').replaceChildren(loadingItem());
    $('p-presc-list').replaceChildren(loadingItem());
    try {
      const patient = await getMyPatient();
      const [records, completed] = await Promise.all([
        fetchAll(`/api/patients/${patient.id}/medical-records`),
        fetchAll('/api/appointments', { status: 'completed' }),
        ensureDoctors(),
      ]);
      await Promise.all(unique(records.map((r) => r.doctor_id)).map((id) => getDoctor(id)));
      renderRecords($('p-rec-list'), records);

      // Рецепты лежат под записями: берём последние 30 состоявшихся приёмов.
      const recent = completed.slice(-30).reverse();
      const groups = await Promise.all(recent.map(async (a) => ({
        appointment: a,
        items: await api(`/api/appointments/${a.id}/prescriptions`),
      })));
      await Promise.all(unique(recent.map((a) => a.doctor_id)).map((id) => getDoctor(id)));
      const withItems = groups.filter((g) => g.items.length);
      fillList($('p-presc-list'), withItems, (g) => {
        const li = card(`Приём ${formatDateTime(g.appointment.starts_at)}`, null,
          [['Врач', doctorName(g.appointment.doctor_id)]]);
        li.querySelector('.extras').append(prescriptionList(g.items));
        return li;
      }, 'Рецептов нет.');
    } catch (error) {
      $('p-rec-list').replaceChildren();
      $('p-presc-list').replaceChildren();
      showError(err, error);
    }
  }
  VIEW_LOADERS['p-records'] = loadPatientRecords;

  /** Назначения (препарат, дозировка, указания) маркированным списком. */
  function prescriptionList(items) {
    return h('ul', { class: 'sublist' }, ...items.map((p) => h('li', {
      text: `${p.medication}, ${p.dosage}${p.instructions ? `. ${p.instructions}` : ''} (выдан ${formatDateTime(p.issued_at)})`,
    })));
  }

  async function loadPatientProfile() {
    const box = $('p-profile');
    const err = $('p-profile-error');
    clearError(err);
    box.replaceChildren();
    try {
      const patient = await getMyPatient();
      box.append(kvList([
        ['ФИО', patient.full_name],
        ['Email', state.user.email],
        ['Дата рождения', formatDate(patient.birth_date)],
        ['Телефон', patient.phone || 'не указан'],
        ['Номер полиса', patient.policy_number],
      ]));
    } catch (error) {
      showError(err, error);
    }
  }
  VIEW_LOADERS['p-profile'] = loadPatientProfile;

  /* ======================================================================
   * 8. Разделы врача
   * ==================================================================== */

  let scheduleRequest = 0;

  /** Отметка результата приёма: action = 'complete' | 'no-show'. */
  async function setAppointmentResult(appointment, action) {
    await api(`/api/appointments/${appointment.id}/${action}`, { method: 'POST' });
    notify(action === 'complete' ? 'Приём отмечен как состоявшийся' : 'Отмечена неявка пациента');
    await loadDoctorSchedule();
  }

  /** Подгружает назначения по приёму в контейнер. */
  async function showPrescriptions(appointmentId, target) {
    try {
      const items = await api(`/api/appointments/${appointmentId}/prescriptions`);
      target.replaceChildren(...(items.length ? [h('h4', { text: 'Назначения' }), prescriptionList(items)] : []));
    } catch (error) {
      target.replaceChildren(h('p', { class: 'form-error', role: 'alert', text: errorText(error) }));
    }
  }

  function diagnosisForm(appointment, doctor) {
    return inlineForm({
      title: 'Диагноз (МКБ-10)',
      submitText: 'Сохранить диагноз',
      fields: [
        { name: 'code', label: 'Код МКБ-10', required: true, maxlength: 6, pattern: '[A-Za-z][0-9]{2}(\\.[0-9]{1,2})?', hint: 'Например J06.9 или I10.' },
        { name: 'notes', label: 'Заметки врача', textarea: true, maxlength: 10000 },
      ],
      onSubmit: async (values, form) => {
        await api('/api/medical-records', {
          method: 'POST',
          body: {
            patient_id: appointment.patient_id,
            doctor_id: doctor.id,
            appointment_id: appointment.id,
            diagnosis_code: values.code,
            notes: values.notes || undefined,
          },
        });
        notify('Диагноз добавлен в медкарту');
        form.remove();
      },
    });
  }

  function prescriptionForm(appointment, onDone) {
    return inlineForm({
      title: 'Рецепт',
      submitText: 'Выписать рецепт',
      fields: [
        { name: 'medication', label: 'Препарат', required: true, maxlength: 255 },
        { name: 'dosage', label: 'Дозировка', required: true, maxlength: 100 },
        { name: 'instructions', label: 'Указания (необязательно)', textarea: true, maxlength: 2000 },
      ],
      onSubmit: async (values, form) => {
        await api('/api/prescriptions', {
          method: 'POST',
          body: {
            appointment_id: appointment.id,
            medication: values.medication,
            dosage: values.dosage,
            instructions: values.instructions || undefined,
          },
        });
        notify('Рецепт выписан');
        form.remove();
        await onDone();
      },
    });
  }

  function doctorAppointmentCard(a, doctor) {
    const actions = [];
    if (a.status === 'scheduled') {
      actions.push(
        btn('Приём состоялся', act(() => setAppointmentResult(a, 'complete')), 'btn'),
        btn('Неявка', act(() => setAppointmentResult(a, 'no-show')), 'btn-danger'));
    }
    if (a.status === 'completed') {
      actions.push(
        btn('Добавить диагноз', (e) => togglePanel(cardExtras(e.currentTarget), 'diagnosis', () => diagnosisForm(a, doctor))),
        btn('Добавить рецепт', (e) => {
          const extras = cardExtras(e.currentTarget);
          togglePanel(extras, 'prescription', () => prescriptionForm(a, () => showPrescriptions(a.id, extras.querySelector('.presc'))));
        }));
    }
    if (a.status !== 'cancelled') {
      actions.push(btn('Медкарта пациента', () => {
        state.pendingPatientId = a.patient_id;
        showView('d-patients');
      }));
    }
    const li = card(`${formatTime(a.starts_at)} – ${formatTime(a.ends_at)}`, statusBadge(a.status), [
      ['Пациент', patientName(a.patient_id)],
      ['Причина', a.reason],
    ], actions);
    if (a.status === 'completed') {
      const holder = h('div', { class: 'presc' });
      li.querySelector('.extras').append(holder);
      showPrescriptions(a.id, holder);
    }
    return li;
  }

  async function loadDoctorSchedule() {
    const list = $('d-list');
    const err = $('d-error');
    const dateInput = $('d-date');
    if (!dateInput.value) dateInput.value = todayYmd();
    const mine = ++scheduleRequest;
    clearError(err);
    list.replaceChildren(loadingItem());
    try {
      const doctor = await getMyDoctor();
      if (!doctor) throw new UiError('Профиль врача не найден или аккаунт неактивен. Обратитесь к администратору.');
      const day = dateInput.value;
      const appointments = await fetchAll('/api/appointments', { date_from: day, date_to: day });
      await Promise.all(unique(appointments.map((a) => a.patient_id)).map((id) => getPatient(id)));
      if (mine !== scheduleRequest) return;
      fillList(list, appointments, (a) => doctorAppointmentCard(a, doctor), 'На выбранный день записей нет.');
    } catch (error) {
      if (mine !== scheduleRequest) return;
      list.replaceChildren();
      showError(err, error);
    }
  }
  VIEW_LOADERS['d-schedule'] = loadDoctorSchedule;

  function bindDoctorSchedule() {
    const shift = (days) => {
      const input = $('d-date');
      input.value = addDays(input.value || todayYmd(), days);
      loadDoctorSchedule();
    };
    $('d-prev').addEventListener('click', () => shift(-1));
    $('d-next').addEventListener('click', () => shift(1));
    $('d-today').addEventListener('click', () => { $('d-date').value = todayYmd(); loadDoctorSchedule(); });
    $('d-date').addEventListener('change', loadDoctorSchedule);
  }

  let patientCardRequest = 0;

  /** Данные пациента и его медкарта (для врача). */
  async function showDoctorPatient(patientId) {
    const info = $('d-patient-info');
    const list = $('d-rec-list');
    const err = $('d-pat-error');
    const mine = ++patientCardRequest;
    clearError(err);
    info.hidden = true;
    list.replaceChildren();
    if (!patientId) return;
    list.replaceChildren(loadingItem());
    try {
      const [patient, records] = await Promise.all([getPatient(patientId), fetchAll(`/api/patients/${patientId}/medical-records`)]);
      await Promise.all(unique(records.map((r) => r.doctor_id)).map((id) => getDoctor(id)));
      if (mine !== patientCardRequest) return;
      if (patient) {
        info.replaceChildren(kvList([
          ['ФИО', patient.full_name],
          ['Дата рождения', formatDate(patient.birth_date)],
          ['Телефон', patient.phone],
          ['Номер полиса', patient.policy_number],
        ]));
        info.hidden = false;
      }
      renderRecords(list, records);
    } catch (error) {
      if (mine !== patientCardRequest) return;
      list.replaceChildren();
      showError(err, error);
    }
  }

  async function loadDoctorPatients() {
    const select = $('d-patient-select');
    const err = $('d-pat-error');
    clearError(err);
    try {
      // Сервер сам ограничивает список пациентами этого врача. Раньше здесь выгружались
      // все записи врача (не больше 500, от старых к новым) и пациенты запрашивались по
      // одному: у загруженного врача новые пациенты молча пропадали из списка.
      const patients = (await fetchAll('/api/patients', {}, 5000))
        .sort((a, b) => a.full_name.localeCompare(b.full_name, 'ru'));
      patients.forEach((p) => state.patientMap.set(p.id, p));
      select.replaceChildren(h('option', { value: '', text: '— выберите пациента —' }),
        ...patients.map((p) => h('option', { value: String(p.id), text: `${p.full_name}, ${formatDate(p.birth_date)}` })));
      const wanted = state.pendingPatientId;
      state.pendingPatientId = null;
      if (wanted && patients.some((p) => p.id === wanted)) select.value = String(wanted);
      await showDoctorPatient(Number(select.value));
    } catch (error) {
      showError(err, error);
    }
  }
  VIEW_LOADERS['d-patients'] = loadDoctorPatients;

  /* ======================================================================
   * 9. Разделы регистратора (доступны и администратору)
   * ==================================================================== */

  /** Панель переноса записи: дата + свободные слоты врача. */
  function reschedulePanel(a) {
    const id = uid();
    const dateInput = h('input', { type: 'date', id, value: localYmd(a.starts_at), min: todayYmd(), required: true });
    const slotBox = h('div', { class: 'slot-box', 'aria-live': 'polite' });
    const err = h('p', { class: 'form-error', role: 'alert', hidden: true });
    let picked = null;
    const refresh = () => renderSlots(slotBox, a.doctor_id, dateInput.value, (slot) => { picked = slot; });
    dateInput.addEventListener('change', refresh);
    const confirmButton = btn('Перенести запись', () => run(confirmButton, err, async () => {
      if (!picked) throw new UiError('Выберите новое время.');
      await api(`/api/appointments/${a.id}`, {
        method: 'PATCH',
        body: { starts_at: toIso(picked.starts_at), ends_at: toIso(picked.ends_at) },
      });
      notify('Запись перенесена');
      await loadStaffAppointments();
    }), 'btn');
    refresh();
    return h('div', { class: 'inline-form' },
      h('h4', { text: 'Перенос записи' }),
      h('div', { class: 'field' }, h('label', { for: id, text: 'Новая дата' }), dateInput),
      slotBox, err, h('div', { class: 'actions' }, confirmButton));
  }

  function staffAppointmentCard(a) {
    const doctor = state.doctorMap.get(a.doctor_id);
    const actions = a.status === 'scheduled' ? [
      btn('Перенести', (e) => togglePanel(cardExtras(e.currentTarget), 'move', () => reschedulePanel(a))),
      btn('Отменить', act(() => cancelAppointment(a, loadStaffAppointments)), 'btn-danger'),
    ] : [];
    return card(formatDateTime(a.starts_at), statusBadge(a.status), [
      ['Пациент', patientName(a.patient_id)],
      ['Врач', doctorName(a.doctor_id)],
      ['Специальность', doctor && doctor.specialty],
      ['Причина', a.reason],
    ], actions);
  }

  let staffRequest = 0;

  async function loadStaffAppointments() {
    const form = $('form-rapp');
    const f = form.elements;
    const err = errorNode(form);
    const list = $('ra-list');
    const mine = ++staffRequest;
    clearError(err);
    list.replaceChildren(loadingItem());
    try {
      await ensureDoctors();
      if (f.doctor.options.length <= 1) {
        f.doctor.replaceChildren(h('option', { value: '', text: 'Все врачи' }),
          ...state.doctors.map((d) => h('option', { value: String(d.id), text: `${d.full_name} (${d.specialty})` })));
      }
      if (!f.date_from.value) f.date_from.value = todayYmd();
      if (!f.date_to.value) f.date_to.value = addDays(f.date_from.value, 7);
      const appointments = await fetchAll('/api/appointments', {
        doctor_id: f.doctor.value, status: f.status.value, date_from: f.date_from.value, date_to: f.date_to.value,
      });
      await Promise.all([
        ...unique(appointments.map((a) => a.patient_id)).map((id) => getPatient(id)),
        ...unique(appointments.map((a) => a.doctor_id)).map((id) => getDoctor(id)),
      ]);
      if (mine !== staffRequest) return;
      fillList(list, appointments, staffAppointmentCard, 'Записей за выбранный период нет.');
    } catch (error) {
      if (mine !== staffRequest) return;
      list.replaceChildren();
      showError(err, error);
    }
  }
  VIEW_LOADERS['r-appointments'] = loadStaffAppointments;

  /** Поиск пациентов и заполнение выпадающего списка (экран записи). */
  async function searchPatientsForBooking() {
    const form = $('form-book');
    const select = $('rb-patient');
    const err = errorNode(form);
    clearError(err);
    try {
      const patients = await api('/api/patients', { query: { q: $('rb-q').value.trim(), limit: 50 } });
      for (const p of patients) state.patientMap.set(p.id, p);
      select.replaceChildren(
        h('option', { value: '', text: patients.length ? '— выберите пациента —' : 'Никого не найдено' }),
        ...patients.map((p) => h('option', { value: String(p.id), text: `${p.full_name}, ${formatDate(p.birth_date)}, полис ${p.policy_number}` })));
    } catch (error) {
      showError(err, error);
    }
  }

  let staffBooking = null;

  async function loadStaffBook() {
    const err = errorNode($('form-book'));
    try {
      await staffBooking.prepare();
      if ($('rb-patient').options.length === 0) await searchPatientsForBooking();
    } catch (error) {
      showError(err, error);
    }
  }
  VIEW_LOADERS['r-book'] = loadStaffBook;

  function patientCard(p) {
    return card(p.full_name, null, [
      ['Дата рождения', formatDate(p.birth_date)],
      ['Телефон', p.phone],
      ['Номер полиса', p.policy_number],
      ['Аккаунт', p.user_id ? `привязан (№${p.user_id})` : 'нет'],
    ]);
  }

  let patientsPager = null;
  const loadStaffPatients = () => patientsPager.reset();
  VIEW_LOADERS['r-patients'] = loadStaffPatients;

  function bindStaffForms() {
    // Фильтр записей
    const rapp = $('form-rapp');
    rapp.addEventListener('submit', (event) => { event.preventDefault(); loadStaffAppointments(); });

    // Запись пациента к врачу
    staffBooking = setupBookingForm($('form-book'), () => Number($('rb-patient').value) || null);
    $('rb-search').addEventListener('click', searchPatientsForBooking);
    $('rb-q').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); searchPatientsForBooking(); }
    });

    // Поиск пациентов с постраничной подгрузкой
    const searchForm = $('form-psearch');
    patientsPager = createPagedList({
      list: $('ps-list'),
      more: $('ps-more'),
      errorBox: errorNode(searchForm),
      empty: 'Пациенты не найдены.',
      fetchPage: (offset, limit) => api('/api/patients', { query: { q: $('ps-q').value.trim(), limit, offset } }),
      render: patientCard,
    });
    searchForm.addEventListener('submit', (event) => { event.preventDefault(); patientsPager.reset(); });

    // Создание пациента
    const createForm = $('form-pcreate');
    createForm.addEventListener('submit', (event) => {
      event.preventDefault();
      return run(event.submitter, errorNode(createForm), async () => {
        const f = createForm.elements;
        const created = await api('/api/patients', {
          method: 'POST',
          body: {
            full_name: f.full_name.value.trim(),
            birth_date: f.birth_date.value,
            phone: normalizePhone(f.phone.value),
            policy_number: f.policy_number.value.trim(),
          },
        });
        notify(`Пациент создан: ${created.full_name}`);
        createForm.reset();
        $('ps-q').value = created.policy_number;
        await patientsPager.reset();
      });
    });
  }

  /* ======================================================================
   * 10. Разделы администратора
   * ==================================================================== */

  // ---- 10.1 Пользователи -----------------------------------------------------

  let usersPager = null;

  /** Карточка пользователя со сменой роли и блокировкой; после изменения заменяет сама себя. */
  function userCard(u) {
    const selectId = uid();
    const select = h('select', { id: selectId },
      ...Object.entries(ROLE_LABELS).map(([value, label]) => h('option', { value, text: label, selected: value === u.role })));
    const replaceWith = (updated) => {
      const li = select.closest('.card');
      li.replaceWith(userCard(updated));
    };
    const roleControl = h('div', { class: 'inline-control' },
      h('label', { class: 'sr-only', for: selectId, text: `Роль пользователя ${u.full_name}` }),
      select,
      btn('Сменить роль', act(async () => {
        if (select.value === u.role) throw new UiError('Роль не изменилась.');
        if (!window.confirm(`Сменить роль пользователя ${u.full_name} на «${ROLE_LABELS[select.value]}»?`)) return;
        const updated = await api(`/api/users/${u.id}`, { method: 'PATCH', body: { role: select.value } });
        notify('Роль изменена');
        replaceWith(updated);
      })));
    const toggle = btn(u.is_active ? 'Заблокировать' : 'Разблокировать', act(async () => {
      if (u.is_active && !window.confirm(`Заблокировать пользователя ${u.full_name}?`)) return;
      const updated = await api(`/api/users/${u.id}`, { method: 'PATCH', body: { is_active: !u.is_active } });
      notify(updated.is_active ? 'Пользователь разблокирован' : 'Пользователь заблокирован');
      replaceWith(updated);
    }), u.is_active ? 'btn-danger' : 'btn');
    const badge = h('span', { class: `badge ${u.is_active ? 'badge-ok' : 'badge-bad'}`, text: u.is_active ? 'Активен' : 'Заблокирован' });
    const li = card(u.full_name, badge, [
      ['Email', u.email],
      ['Роль', ROLE_LABELS[u.role] || u.role],
      ['Создан', formatDateTime(u.created_at)],
    ], [roleControl, toggle]);
    return li;
  }

  function bindUsers() {
    const filterForm = $('form-ufilter');
    usersPager = createPagedList({
      list: $('u-list'),
      more: $('u-more'),
      errorBox: errorNode(filterForm),
      empty: 'Пользователи не найдены.',
      fetchPage: (offset, limit) => api('/api/users', {
        query: { q: filterForm.elements.q.value.trim(), role: filterForm.elements.role.value, is_active: filterForm.elements.active.value, limit, offset },
      }),
      render: userCard,
    });
    filterForm.addEventListener('submit', (event) => { event.preventDefault(); usersPager.reset(); });

    const createForm = $('form-ucreate');
    createForm.addEventListener('submit', (event) => {
      event.preventDefault();
      return run(event.submitter, errorNode(createForm), async () => {
        const f = createForm.elements;
        const created = await api('/api/users', {
          method: 'POST',
          body: { full_name: f.full_name.value.trim(), email: f.email.value.trim(), password: f.password.value, role: f.role.value },
        });
        notify(`Пользователь создан: ${created.full_name}`);
        createForm.reset();
        await usersPager.reset();
      });
    });
  }
  VIEW_LOADERS['a-users'] = () => usersPager.reset();

  // ---- 10.2 Профили врачей и графики -----------------------------------------------

  const hhmm = (time) => String(time).slice(0, 5);

  /** Список пользователей-врачей без профиля в поле формы. */
  function fillDoctorUserSelect() {
    const select = $('form-doctor').elements.user_id;
    const free = state.doctorUsers.filter((u) => u.is_active && !state.doctors.some((d) => d.user_id === u.id));
    select.replaceChildren(
      h('option', { value: '', text: free.length ? '— выберите пользователя —' : 'Нет врачей без профиля' }),
      ...free.map((u) => h('option', { value: String(u.id), text: `${u.full_name} (${u.email})` })));
    select.disabled = false;
  }

  /** Возвращает форму профиля в режим создания. */
  function resetDoctorForm() {
    const form = $('form-doctor');
    state.editDoctor = null;
    form.reset();
    clearError(errorNode(form));
    $('doc-form-title').textContent = 'Новый профиль врача';
    $('df-submit').textContent = 'Создать профиль';
    $('df-cancel').hidden = true;
    fillDoctorUserSelect();
  }

  /** Переводит форму в режим изменения выбранного врача. */
  function beginEditDoctor(d) {
    const form = $('form-doctor');
    const f = form.elements;
    state.editDoctor = d;
    clearError(errorNode(form));
    $('doc-form-title').textContent = `Изменение: ${d.full_name}`;
    $('df-submit').textContent = 'Сохранить изменения';
    $('df-cancel').hidden = false;
    f.user_id.replaceChildren(h('option', { value: String(d.user_id), text: d.full_name }));
    f.user_id.disabled = true;
    f.specialty.value = d.specialty;
    f.cabinet.value = d.cabinet;
    f.slot_minutes.value = d.slot_minutes;
    f.work_start.value = hhmm(d.work_start);
    f.work_end.value = hhmm(d.work_end);
    for (const box of form.querySelectorAll('input[name="days"]')) box.checked = d.work_days.includes(Number(box.value));
    f.specialty.focus();
    form.scrollIntoView({ block: 'center' });
  }

  function doctorCard(d) {
    return card(d.full_name, h('span', { class: 'badge badge-scheduled', text: d.specialty }), [
      ['Кабинет', d.cabinet],
      ['Длительность приёма', `${d.slot_minutes} мин`],
      ['Смена (МСК)', `${hhmm(d.work_start)} – ${hhmm(d.work_end)}`],
      ['Рабочие дни', d.work_days.map((n) => DAY_NAMES[n - 1]).join(', ')],
    ], [btn('Изменить график и данные', () => beginEditDoctor(d))]);
  }

  async function loadAdminDoctors() {
    const list = $('doc-list');
    const err = $('doc-error');
    clearError(err);
    list.replaceChildren(loadingItem());
    try {
      const [doctors, users] = await Promise.all([ensureDoctors(true), fetchAll('/api/users', { role: 'doctor' })]);
      state.doctorUsers = users;
      fillList(list, doctors, doctorCard, 'Профилей врачей пока нет.');
      if (!state.editDoctor) fillDoctorUserSelect();
    } catch (error) {
      list.replaceChildren();
      showError(err, error);
    }
  }
  VIEW_LOADERS['a-doctors'] = loadAdminDoctors;

  function bindDoctorForm() {
    const form = $('form-doctor');
    $('df-cancel').addEventListener('click', resetDoctorForm);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      return run(event.submitter, errorNode(form), async () => {
        const f = form.elements;
        const days = [...form.querySelectorAll('input[name="days"]:checked')].map((box) => Number(box.value));
        if (!days.length) throw new UiError('Выберите хотя бы один рабочий день.');
        const values = {
          specialty: f.specialty.value.trim(),
          cabinet: f.cabinet.value.trim(),
          slot_minutes: Number(f.slot_minutes.value),
          work_start: f.work_start.value,
          work_end: f.work_end.value,
          work_days: days,
        };
        const edited = state.editDoctor;
        if (!edited) {
          if (!f.user_id.value) throw new UiError('Выберите пользователя.');
          await api('/api/doctors', { method: 'POST', body: { user_id: Number(f.user_id.value), ...values } });
          notify('Профиль врача создан');
        } else {
          // PATCH: отправляем только изменённые поля.
          const changes = {};
          const original = { ...edited, work_start: hhmm(edited.work_start), work_end: hhmm(edited.work_end) };
          for (const [key, value] of Object.entries(values)) {
            if (JSON.stringify(value) !== JSON.stringify(original[key])) changes[key] = value;
          }
          if (!Object.keys(changes).length) throw new UiError('Нет изменений для сохранения.');
          await api(`/api/doctors/${edited.id}`, { method: 'PATCH', body: changes });
          notify('Профиль врача обновлён');
        }
        resetDoctorForm();
        await loadAdminDoctors();
      });
    });
  }

  // ---- 10.3 SVG-графики ----------------------------------------------------------------

  /** Шкала оси: шаг и верхняя граница; integer — шаг не меньше 1. */
  function niceScale(max, integer) {
    if (!(max > 0)) return { step: 1, top: 4 };
    const raw = max / 4;
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const ratio = raw / magnitude;
    let step = (ratio <= 1 ? 1 : ratio <= 2 ? 2 : ratio <= 5 ? 5 : 10) * magnitude;
    if (integer) step = Math.max(1, Math.ceil(step));
    return { step, top: Math.ceil(max / step) * step };
  }

  const shorten = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

  /** Корневой <svg> с названием и описанием для скринридеров. */
  function chartRoot(width, height, title, desc) {
    const titleId = uid();
    const descId = uid();
    return svg('svg', {
      viewBox: `0 0 ${width} ${height}`,
      width,
      class: width > 560 ? 'chart chart-wide' : 'chart',
      role: 'img',
      'aria-labelledby': `${titleId} ${descId}`,
    }, svg('title', { id: titleId, text: title }), svg('desc', { id: descId, text: desc }));
  }

  /** Вертикальная столбчатая диаграмма. items: [{label, value, cls?}]. */
  function barChartVertical(items, { title, desc, slot = 40, integer = true, format = String }) {
    const plotH = 180;
    const m = { l: 44, r: 12, t: 22, b: 58 };
    const width = m.l + m.r + Math.max(items.length * slot, 220);
    const height = m.t + plotH + m.b;
    const { step, top } = niceScale(Math.max(0, ...items.map((i) => i.value)), integer);
    const root = chartRoot(width, height, title, desc);
    for (let i = 0; i <= Math.round(top / step); i += 1) {
      const value = i * step;
      const y = m.t + plotH - (value / top) * plotH;
      root.append(
        svg('line', { class: i === 0 ? 'axis' : 'grid', x1: m.l, x2: width - m.r, y1: y, y2: y }),
        svg('text', { class: 'tick', x: m.l - 6, y: y + 4, 'text-anchor': 'end', text: format(value) }));
    }
    const rotate = items.some((i) => shorten(i.label, 14).length * 6.2 > slot);
    items.forEach((item, index) => {
      const barW = slot * 0.7;
      const x = m.l + index * slot + slot * 0.15;
      const barH = (item.value / top) * plotH;
      const y = m.t + plotH - barH;
      root.append(svg('rect', { class: `bar ${item.cls || ''}`.trim(), x, y, width: barW, height: Math.max(barH, item.value > 0 ? 1 : 0) },
        svg('title', { text: `${item.label}: ${format(item.value)}` })));
      if (slot >= 28) {
        root.append(svg('text', { class: 'value', x: x + barW / 2, y: y - 4, 'text-anchor': 'middle', text: format(item.value) }));
      }
      const lx = m.l + index * slot + slot / 2;
      const ly = m.t + plotH + 14;
      root.append(svg('text', rotate
        ? { x: lx, y: ly, 'text-anchor': 'end', transform: `rotate(-40 ${lx} ${ly})`, text: shorten(item.label, 14) }
        : { x: lx, y: ly, 'text-anchor': 'middle', text: item.label }));
    });
    return root;
  }

  /** Горизонтальная столбчатая диаграмма (длинные подписи слева). */
  function barChartHorizontal(items, { title, desc, integer = false, format = String }) {
    const rowH = 30;
    const m = { l: 140, r: 56, t: 6, b: 26 };
    const plotW = 330;
    const width = m.l + plotW + m.r;
    const height = m.t + items.length * rowH + m.b;
    const { step, top } = niceScale(Math.max(0, ...items.map((i) => i.value)), integer);
    const root = chartRoot(width, height, title, desc);
    for (let i = 0; i <= Math.round(top / step); i += 1) {
      const value = i * step;
      const x = m.l + (value / top) * plotW;
      root.append(
        svg('line', { class: i === 0 ? 'axis' : 'grid', x1: x, x2: x, y1: m.t, y2: height - m.b }),
        svg('text', { class: 'tick', x, y: height - m.b + 16, 'text-anchor': 'middle', text: format(value) }));
    }
    items.forEach((item, index) => {
      const y = m.t + index * rowH + 5;
      const barW = (item.value / top) * plotW;
      root.append(
        svg('text', { x: m.l - 8, y: y + 14, 'text-anchor': 'end', text: shorten(item.label, 20) }, svg('title', { text: item.label })),
        svg('rect', { class: 'bar', x: m.l, y, width: Math.max(barW, item.value > 0 ? 1 : 0), height: rowH - 10 },
          svg('title', { text: `${item.label}: ${format(item.value)}` })),
        svg('text', { class: 'value', x: m.l + barW + 6, y: y + 14, text: format(item.value) }));
    });
    return root;
  }

  /** Блок «заголовок + диаграмма с прокруткой» либо сообщение об отсутствии данных. */
  function chartBox(caption, items, build) {
    const body = items.length
      ? h('div', { class: 'chart-scroll', role: 'group', 'aria-label': caption, tabindex: '0' }, build())
      : h('p', { class: 'muted', text: 'Нет данных за выбранный период.' });
    return h('figure', { class: 'chart-box' }, h('figcaption', { text: caption }), body);
  }

  const percent = (rate) => `${numberFmt.format(rate * 100)} %`;
  const kpi = (value, label) => h('div', { class: 'kpi' }, h('span', { class: 'kpi-value', text: String(value) }), h('span', { class: 'kpi-label', text: label }));

  /** Таблица загрузки врачей (дублирует диаграмму в текстовом виде). */
  function loadTable(rows) {
    const headers = ['Врач', 'Загрузка', 'Занято, мин', 'Доступно, мин', 'Состоялось', 'Неявок'];
    const body = rows.map((r) => h('tr', {},
      h('th', { scope: 'row', text: `${doctorName(r.doctor_id)} (${r.specialty})${r.is_active ? '' : ', неактивен'}` }),
      h('td', { class: 'num', text: `${numberFmt.format(r.load_percent)} %` }),
      h('td', { class: 'num', text: String(r.busy_minutes) }),
      h('td', { class: 'num', text: String(r.available_minutes) }),
      h('td', { class: 'num', text: String(r.completed) }),
      h('td', { class: 'num', text: String(r.no_show) })));
    return h('div', { class: 'table-wrap', role: 'region', 'aria-label': 'Загрузка врачей, таблица', tabindex: '0' },
      h('table', {},
        h('caption', { class: 'sr-only', text: 'Загрузка врачей за период' }),
        h('thead', {}, h('tr', {}, ...headers.map((t) => h('th', { scope: 'col', text: t })))),
        h('tbody', {}, ...body)));
  }

  /** Строит весь дашборд из четырёх ответов аналитики. */
  function renderDashboard(out, summary, load, perDay, top) {
    const s = summary.by_status;
    const period = `${formatDate(summary.date_from)} – ${formatDate(summary.date_to)}`;
    const statusItems = ['scheduled', 'completed', 'cancelled', 'no_show']
      .map((key) => ({ label: STATUS_LABELS[key], value: s[key], cls: `bar-${key}` }));
    const loadItems = load.map((r) => ({ label: `${doctorName(r.doctor_id)}`, value: Math.round(r.load_percent * 10) / 10 }));
    const dayItems = perDay.map((d) => ({ label: `${d.day.slice(8, 10)}.${d.day.slice(5, 7)}`, value: d.count }));
    const diagItems = top.map((d) => ({ label: d.code, value: d.count }));

    const blocks = [
      h('div', { class: 'kpis' },
        kpi(summary.total, `записей за ${period}`),
        kpi(percent(summary.no_show_rate), 'доля неявок'),
        kpi(percent(summary.cancellation_rate), 'доля отмен')),
      chartBox('Записи по статусам', statusItems, () => barChartVertical(statusItems, {
        title: 'Записи по статусам', slot: 96,
        desc: statusItems.map((i) => `${i.label}: ${i.value}`).join('; '),
      })),
      chartBox('Загрузка врачей, %', loadItems, () => barChartHorizontal(loadItems, {
        title: 'Загрузка врачей, %', format: (v) => `${numberFmt.format(v)}`,
        desc: loadItems.map((i) => `${i.label}: ${i.value} %`).join('; '),
      })),
      load.length ? loadTable(load) : null,
      chartBox('Записи по дням', dayItems, () => barChartVertical(dayItems, {
        title: 'Записи по дням', slot: dayItems.length > 12 ? 22 : 48,
        desc: `Всего дней: ${dayItems.length}; записей: ${dayItems.reduce((sum, i) => sum + i.value, 0)}`,
      })),
      chartBox('Частые диагнозы (МКБ-10)', diagItems, () => barChartVertical(diagItems, {
        title: 'Частые диагнозы', slot: 56,
        desc: diagItems.map((i) => `${i.label}: ${i.value}`).join('; '),
      })),
    ];
    out.replaceChildren(...blocks.filter(Boolean));
  }

  async function loadDashboard() {
    const form = $('form-dash');
    const f = form.elements;
    const err = errorNode(form);
    const out = $('dash-out');
    clearError(err);
    if (!f.date_to.value) f.date_to.value = todayYmd();
    if (!f.date_from.value) f.date_from.value = addDays(f.date_to.value, -29);
    if (f.date_from.value > f.date_to.value) {
      showError(err, new UiError('Дата «с» не может быть позже даты «по».'));
      return;
    }
    const query = { date_from: f.date_from.value, date_to: f.date_to.value };
    out.replaceChildren(h('p', { class: 'muted', text: 'Загрузка…' }));
    try {
      const [summary, load, perDay, top] = await Promise.all([
        api('/api/analytics/summary', { query }),
        api('/api/analytics/doctors-load', { query }),
        api('/api/analytics/appointments-per-day', { query }),
        api('/api/analytics/top-diagnoses', { query: { ...query, limit: 10 } }),
        ensureDoctors(),
      ]);
      await Promise.all(unique(load.map((r) => r.doctor_id)).map((id) => getDoctor(id)));
      renderDashboard(out, summary, load, perDay, top);
    } catch (error) {
      out.replaceChildren();
      showError(err, error);
    }
  }
  VIEW_LOADERS['a-dashboard'] = loadDashboard;

  /* ======================================================================
   * Запуск
   * ==================================================================== */

  function bindStaticHandlers() {
    bindAuth();
    bindDoctorSchedule();
    bindStaffForms();
    bindUsers();
    bindDoctorForm();
    newBooking = patientBooking();
    $('p-app-filter').addEventListener('change', loadPatientAppointments);
    $('d-patient-select').addEventListener('change', (event) => showDoctorPatient(Number(event.target.value)));
    $('form-dash').addEventListener('submit', (event) => { event.preventDefault(); loadDashboard(); });
  }

  /** Восстанавливает сессию из sessionStorage или показывает экран входа. */
  async function init() {
    bindStaticHandlers();
    const saved = storageGet();
    if (!saved) {
      showAuth('');
      return;
    }
    try {
      await startSession(saved);
    } catch (error) {
      // При 401 сообщение уже показано handleUnauthorized.
      if (!(error instanceof ApiError && error.status === 401)) showAuth(errorText(error));
    }
  }

  init();
})();
