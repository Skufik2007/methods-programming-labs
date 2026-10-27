// Потоковый клиент Ollama /api/chat на встроенном fetch (Node 18+). Без зависимости от vscode.

import type { ChatMessage } from './prompt';

export type StreamEvent =
  | { type: 'token'; text: string }
  | { type: 'done' }
  | { type: 'error'; message: string };

/**
 * Инкрементальный разбор NDJSON: чанки сети не совпадают с границами строк,
 * поэтому неполный «хвост» копится в буфере до прихода '\n'.
 */
export class NdjsonParser {
  private buffer = '';
  private finished = false;

  /** Добавляет очередной кусок текста и возвращает события из всех завершённых строк. */
  push(chunk: string): StreamEvent[] {
    this.buffer += chunk;
    const events: StreamEvent[] = [];
    let idx: number;
    while ((idx = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 1);
      this.parseLine(line, events);
    }
    return events;
  }

  /** Вызывается в конце потока: разбирает последнюю строку без завершающего '\n'. */
  flush(): StreamEvent[] {
    const events: StreamEvent[] = [];
    const rest = this.buffer;
    this.buffer = '';
    this.parseLine(rest, events);
    return events;
  }

  private parseLine(rawLine: string, out: StreamEvent[]): void {
    if (this.finished) {
      return;
    }
    const line = rawLine.trim(); // убирает и '\r' от CRLF
    if (line === '') {
      return;
    }
    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      this.finished = true;
      out.push({ type: 'error', message: `Некорректная строка в потоке Ollama: ${line.slice(0, 120)}` });
      return;
    }
    if (typeof obj !== 'object' || obj === null) {
      return;
    }
    const rec = obj as Record<string, unknown>;

    // Ошибка посреди потока приходит как {"error": "..."}.
    if (typeof rec.error === 'string') {
      this.finished = true;
      out.push({ type: 'error', message: rec.error });
      return;
    }

    const message = rec.message;
    if (typeof message === 'object' && message !== null) {
      const content = (message as Record<string, unknown>).content;
      if (typeof content === 'string' && content.length > 0) {
        out.push({ type: 'token', text: content });
      }
    }
    // Последний объект: done: true (в нём тоже может быть message.content — он учтён выше).
    if (rec.done === true) {
      this.finished = true;
      out.push({ type: 'done' });
    }
  }
}

export type OllamaErrorKind = 'unreachable' | 'model-missing' | 'http' | 'stream';

export class OllamaError extends Error {
  constructor(
    public readonly kind: OllamaErrorKind,
    message: string
  ) {
    super(message);
    this.name = 'OllamaError';
  }
}

export interface StreamChatOptions {
  baseUrl: string;
  model: string;
  messages: ChatMessage[];
  temperature: number;
  signal?: AbortSignal;
  onToken: (text: string) => void;
  /** Подмена fetch для тестов. */
  fetchImpl?: typeof fetch;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === 'AbortError';
}

/** Node fetch при недоступном сервере бросает TypeError('fetch failed') с cause.code = ECONNREFUSED. */
export function isConnectionError(err: unknown): boolean {
  if (!(err instanceof Error)) {
    return false;
  }
  const cause = (err as { cause?: { code?: string } }).cause;
  const code = cause?.code;
  if (code === 'ECONNREFUSED' || code === 'ENOTFOUND' || code === 'ECONNRESET' || code === 'EHOSTUNREACH') {
    return true;
  }
  return /fetch failed|ECONNREFUSED/i.test(err.message);
}

function normalizeBaseUrl(url: string): string {
  return url.trim().replace(/\/+$/, '');
}

/** Стримит ответ модели; каждый токен отдаёт в onToken. Завершается при done: true. */
export async function streamChat(opts: StreamChatOptions): Promise<void> {
  const doFetch = opts.fetchImpl ?? fetch;
  const base = normalizeBaseUrl(opts.baseUrl);
  let res: Response;
  try {
    res = await doFetch(`${base}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: opts.model,
        messages: opts.messages,
        stream: true,
        options: { temperature: opts.temperature }
      }),
      signal: opts.signal
    });
  } catch (err) {
    if (isAbortError(err)) {
      throw err;
    }
    if (isConnectionError(err)) {
      throw new OllamaError('unreachable', `Не удалось подключиться к Ollama по адресу ${base}.`);
    }
    throw err;
  }

  if (!res.ok) {
    let detail = '';
    try {
      const text = await res.text();
      try {
        const parsed = JSON.parse(text) as { error?: string };
        detail = parsed.error ?? text;
      } catch {
        detail = text;
      }
    } catch {
      // тело ответа не прочиталось — оставляем пустую деталь
    }
    if (res.status === 404) {
      throw new OllamaError('model-missing', detail || `Модель ${opts.model} не найдена.`);
    }
    throw new OllamaError('http', `Ollama вернула HTTP ${res.status}${detail ? `: ${detail}` : ''}`);
  }
  if (!res.body) {
    throw new OllamaError('stream', 'Ответ Ollama не содержит тела.');
  }

  const reader = res.body.getReader();
  // stream: true — многобайтный символ UTF-8 может быть разрезан между чанками.
  const decoder = new TextDecoder('utf-8');
  const parser = new NdjsonParser();

  // Возвращает true, когда поток завершён (done).
  const handle = (events: StreamEvent[]): boolean => {
    for (const ev of events) {
      if (ev.type === 'token') {
        opts.onToken(ev.text);
      } else if (ev.type === 'error') {
        throw new OllamaError('stream', ev.message);
      } else {
        return true;
      }
    }
    return false;
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      if (handle(parser.push(decoder.decode(value, { stream: true })))) {
        return;
      }
    }
    if (handle(parser.push(decoder.decode()))) {
      return;
    }
    if (handle(parser.flush())) {
      return;
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
  throw new OllamaError('stream', 'Поток ответа оборвался до сообщения done.');
}

/** Человекочитаемое сообщение об ошибке для пользователя. */
export function describeError(err: unknown, ctx: { baseUrl: string; model: string }): string {
  if (err instanceof OllamaError) {
    switch (err.kind) {
      case 'unreachable':
        return (
          `Ollama не запущена или недоступна (${ctx.baseUrl}). ` +
          'Запустите сервер командой «ollama serve» (или откройте приложение Ollama) и повторите.'
        );
      case 'model-missing':
        return `Модель «${ctx.model}» не найдена. Загрузите её командой: ollama pull ${ctx.model}`;
      default:
        return err.message;
    }
  }
  if (isConnectionError(err)) {
    return describeError(new OllamaError('unreachable', ''), ctx);
  }
  return err instanceof Error ? err.message : String(err);
}
