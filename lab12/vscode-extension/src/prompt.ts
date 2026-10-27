// Чистые функции без зависимости от vscode — их можно тестировать через node:test.

export type ChatRole = 'system' | 'user' | 'assistant';

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

/** Максимальная длина кода, отправляемого модели (символы). */
export const MAX_CODE_LENGTH = 12_000;

/** Сколько строк брать вокруг курсора, если нет выделения. */
export const CONTEXT_RADIUS = 10;

export const DEFAULT_SYSTEM_PROMPT = [
  'Ты — опытный разработчик и наставник. Объясняй присланный фрагмент кода на русском языке.',
  'Вместе с кодом тебе сообщают имя файла и язык (languageId) — учитывай их: идиомы, стандартную',
  'библиотеку и типичные ошибки именно этого языка.',
  'Структура ответа (используй именно эти заголовки):',
  '1. Что делает код — 1–3 предложения по сути.',
  '2. Как работает (по шагам) — пронумерованный список шагов.',
  '3. Неочевидные места и возможные ошибки — крайние случаи, побочные эффекты, уязвимости.',
  '4. Как улучшить — конкретные, короткие рекомендации.',
  'Не выдумывай код, которого нет во фрагменте. Если контекста не хватает — так и скажи.',
  'Отвечай кратко и по делу.'
].join('\n');

export interface TruncateResult {
  code: string;
  truncated: boolean;
}

/** Обрезает код до maxLength символов. Не разрезает суррогатную пару пополам. */
export function truncateCode(code: string, maxLength: number = MAX_CODE_LENGTH): TruncateResult {
  if (code.length <= maxLength) {
    return { code, truncated: false };
  }
  let end = maxLength;
  // Если последний оставшийся символ — старшая половина суррогатной пары, отбрасываем её.
  const last = code.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) {
    end -= 1;
  }
  return { code: code.slice(0, end), truncated: true };
}

export interface LineRange {
  /** Первая строка (0-based, включительно). */
  start: number;
  /** Последняя строка (0-based, включительно). */
  end: number;
}

/**
 * Диапазон строк вокруг курсора: строка курсора ± radius, ограниченный границами файла.
 * У начала/конца файла окно просто урезается (не сдвигается).
 */
export function pickContextRange(
  lineCount: number,
  cursorLine: number,
  radius: number = CONTEXT_RADIUS
): LineRange {
  if (lineCount <= 0) {
    return { start: 0, end: 0 };
  }
  const last = lineCount - 1;
  const cursor = Math.min(Math.max(cursorLine, 0), last);
  return {
    start: Math.max(0, cursor - radius),
    end: Math.min(last, cursor + radius)
  };
}

/**
 * Выбирает ограждение (fence) для markdown-блока: тройные кавычки, но если в самом коде
 * встречается серия обратных кавычек такой же или большей длины — берём длиннее,
 * чтобы код не «выпрыгнул» из блока.
 */
export function chooseFence(code: string): string {
  let longest = 0;
  for (const m of code.matchAll(/`+/g)) {
    longest = Math.max(longest, m[0].length);
  }
  return '`'.repeat(Math.max(3, longest + 1));
}

/** Оборачивает код в блок с указанием языка. */
export function wrapCode(code: string, languageId: string): string {
  const fence = chooseFence(code);
  return `${fence}${languageId}\n${code}\n${fence}`;
}

export interface BuildMessagesInput {
  /** Значение настройки clinicAi.systemPrompt; пустая строка — использовать встроенный. */
  systemPrompt?: string;
  languageId: string;
  fileName: string;
  code: string;
  maxCodeLength?: number;
}

export interface BuildMessagesResult {
  messages: ChatMessage[];
  truncated: boolean;
}

/** Собирает сообщения для /api/chat: системный промпт + код в блоке с языком. */
export function buildMessages(input: BuildMessagesInput): BuildMessagesResult {
  const custom = input.systemPrompt?.trim();
  const system = custom ? custom : DEFAULT_SYSTEM_PROMPT;
  const { code, truncated } = truncateCode(input.code, input.maxCodeLength ?? MAX_CODE_LENGTH);

  const lines = [
    `Файл: ${input.fileName}`,
    `Язык: ${input.languageId}`,
    '',
    'Объясни этот код:',
    '',
    wrapCode(code, input.languageId)
  ];
  if (truncated) {
    lines.push('', '(Код был обрезан из-за ограничения длины — объясняй только присланную часть.)');
  }

  return {
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: lines.join('\n') }
    ],
    truncated
  };
}
