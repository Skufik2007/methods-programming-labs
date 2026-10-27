Ниже полный набор файлов расширения «Clinic AI: Explain Code».

### vscode-extension/package.json

````json
{
  "name": "clinic-ai-explain-code",
  "displayName": "Clinic AI: Explain Code",
  "description": "Объясняет выделенный код с помощью локальной модели Ollama (потоковый вывод).",
  "version": "0.1.0",
  "publisher": "chetverikov-lab",
  "license": "MIT",
  "private": true,
  "engines": {
    "vscode": "^1.90.0"
  },
  "categories": [
    "Other"
  ],
  "main": "./out/extension.js",
  "contributes": {
    "commands": [
      {
        "command": "clinicAi.explainCode",
        "title": "Объясни этот код",
        "category": "Clinic AI",
        "icon": "$(sparkle)"
      }
    ],
    "keybindings": [
      {
        "command": "clinicAi.explainCode",
        "key": "ctrl+alt+e",
        "mac": "cmd+alt+e",
        "when": "editorTextFocus"
      }
    ],
    "menus": {
      "editor/context": [
        {
          "command": "clinicAi.explainCode",
          "group": "navigation@100"
        }
      ],
      "editor/title": [
        {
          "command": "clinicAi.explainCode",
          "when": "editorHasSelection",
          "group": "navigation"
        }
      ]
    },
    "configuration": {
      "title": "Clinic AI",
      "properties": {
        "clinicAi.ollamaUrl": {
          "type": "string",
          "default": "http://127.0.0.1:11434",
          "description": "Адрес сервера Ollama."
        },
        "clinicAi.model": {
          "type": "string",
          "default": "qwen2.5-coder:3b",
          "description": "Имя модели Ollama (должна быть загружена командой ollama pull)."
        },
        "clinicAi.systemPrompt": {
          "type": "string",
          "default": "",
          "editPresentation": "multilineText",
          "markdownDescription": "Свой системный промпт. Если пусто — используется встроенный (объяснение на русском по структуре: что делает код, как работает, неочевидные места, как улучшить)."
        },
        "clinicAi.temperature": {
          "type": "number",
          "default": 0.2,
          "minimum": 0,
          "maximum": 1,
          "description": "Температура генерации (0..1)."
        }
      }
    }
  },
  "scripts": {
    "compile": "tsc -p ./",
    "watch": "tsc -watch -p ./",
    "pretest": "tsc -p tsconfig.test.json",
    "test": "node --test out-test/test/prompt.test.js out-test/test/ollama.test.js",
    "package": "vsce package --allow-missing-repository"
  },
  "devDependencies": {
    "@types/node": "^20.14.0",
    "@types/vscode": "^1.90.0",
    "@vscode/vsce": "^3.0.0",
    "typescript": "^5.5.0"
  }
}
````

### vscode-extension/tsconfig.json

````json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "commonjs",
    "lib": ["ES2022"],
    "types": ["node"],
    "outDir": "out",
    "rootDir": "src",
    "strict": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "forceConsistentCasingInFileNames": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "sourceMap": true
  },
  "include": ["src"]
}
````

### vscode-extension/tsconfig.test.json

````json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "outDir": "out-test",
    "rootDir": ".",
    "sourceMap": false
  },
  "include": ["src", "test"]
}
````

### vscode-extension/.vscodeignore

````text
.vscode/**
src/**
test/**
out-test/**
out/**/*.map
tsconfig.json
tsconfig.test.json
node_modules/**
**/*.ts
.gitignore
````

### vscode-extension/src/prompt.ts

````ts
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
````

### vscode-extension/src/ollama.ts

````ts
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
````

### vscode-extension/src/extension.ts

````ts
import * as path from 'node:path';
import * as vscode from 'vscode';
import { describeError, isAbortError, streamChat } from './ollama';
import { buildMessages, pickContextRange } from './prompt';

const VIEW_TYPE = 'clinicAiExplain';

interface PanelState {
  panel: vscode.WebviewPanel;
  /** Webview прислал 'ready' — можно слать сообщения. */
  ready: boolean;
  /** Сообщения, отправленные до готовности webview. */
  queue: unknown[];
  /** Накопленный ответ — для кнопки «Копировать». */
  text: string;
  controller?: AbortController;
  /** Номер запроса: токены устаревших запросов игнорируются. */
  requestId: number;
}

let state: PanelState | undefined;

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand('clinicAi.explainCode', () => explainCode(context))
  );
}

export function deactivate(): void {
  state?.controller?.abort();
}

function post(s: PanelState, message: unknown): void {
  if (s.ready) {
    void s.panel.webview.postMessage(message);
  } else {
    s.queue.push(message);
  }
}

function getNonce(): string {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let out = '';
  for (let i = 0; i < 32; i++) {
    out += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return out;
}

function buildHtml(webview: vscode.Webview, mediaRoot: vscode.Uri): string {
  const nonce = getNonce();
  const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, 'panel.css'));
  const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, 'panel.js'));
  // default-src 'none': запрещено всё, кроме явно разрешённого ниже.
  const csp = [
    "default-src 'none'",
    `style-src ${webview.cspSource}`,
    `script-src 'nonce-${nonce}'`
  ].join('; ');
  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${styleUri}">
  <title>Clinic AI</title>
</head>
<body>
  <header>
    <div id="title">Clinic AI: объяснение кода</div>
    <div class="buttons">
      <button id="stop" type="button" disabled>Стоп</button>
      <button id="copy" type="button" disabled>Копировать</button>
    </div>
  </header>
  <div id="notice" hidden></div>
  <div id="error" hidden></div>
  <pre id="output"></pre>
  <div id="status"></div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}

/** Создаёт панель или переиспользует существующую. */
function ensurePanel(context: vscode.ExtensionContext): PanelState {
  if (state) {
    state.panel.reveal(vscode.ViewColumn.Beside, true);
    return state;
  }
  const mediaRoot = vscode.Uri.joinPath(context.extensionUri, 'media');
  const panel = vscode.window.createWebviewPanel(
    VIEW_TYPE,
    'Clinic AI: объяснение',
    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
    {
      enableScripts: true,
      // Webview может читать ресурсы только из каталога media.
      localResourceRoots: [mediaRoot],
      retainContextWhenHidden: true
    }
  );
  const s: PanelState = { panel, ready: false, queue: [], text: '', requestId: 0 };
  state = s;
  panel.webview.html = buildHtml(panel.webview, mediaRoot);

  panel.webview.onDidReceiveMessage((msg: unknown) => {
    // Сообщения из webview недоверенные: проверяем форму и тип.
    if (typeof msg !== 'object' || msg === null) {
      return;
    }
    const type = (msg as { type?: unknown }).type;
    switch (type) {
      case 'ready':
        s.ready = true;
        for (const m of s.queue.splice(0)) {
          void panel.webview.postMessage(m);
        }
        break;
      case 'stop':
        s.controller?.abort();
        break;
      case 'copy':
        void vscode.env.clipboard.writeText(s.text).then(() => {
          void vscode.window.setStatusBarMessage('Clinic AI: ответ скопирован', 2000);
        });
        break;
      default:
        break;
    }
  });

  panel.onDidDispose(() => {
    s.controller?.abort();
    state = undefined;
  });
  return s;
}

function readCodeFromEditor(editor: vscode.TextEditor): string {
  const doc = editor.document;
  const sel = editor.selection;
  if (!sel.isEmpty) {
    return doc.getText(sel);
  }
  // Нет выделения: текущая строка ± 10 строк.
  const { start, end } = pickContextRange(doc.lineCount, sel.active.line);
  const range = new vscode.Range(start, 0, end, doc.lineAt(end).range.end.character);
  return doc.getText(range);
}

async function explainCode(context: vscode.ExtensionContext): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    void vscode.window.showWarningMessage('Clinic AI: откройте файл с кодом.');
    return;
  }
  const code = readCodeFromEditor(editor);
  if (code.trim() === '') {
    void vscode.window.showInformationMessage('Clinic AI: нет кода для объяснения (пустая область).');
    return;
  }

  const cfg = vscode.workspace.getConfiguration('clinicAi');
  const baseUrl = cfg.get<string>('ollamaUrl', 'http://127.0.0.1:11434');
  const model = cfg.get<string>('model', 'qwen2.5-coder:3b');
  const systemPrompt = cfg.get<string>('systemPrompt', '');
  const rawTemp = cfg.get<number>('temperature', 0.2);
  const temperature = Number.isFinite(rawTemp) ? Math.min(1, Math.max(0, rawTemp)) : 0.2;

  const fileName = path.basename(editor.document.fileName);
  const { messages, truncated } = buildMessages({
    systemPrompt,
    languageId: editor.document.languageId,
    fileName,
    code
  });

  const s = ensurePanel(context);
  // Новый запрос отменяет предыдущий и «обнуляет» панель.
  s.controller?.abort();
  const controller = new AbortController();
  s.controller = controller;
  const requestId = ++s.requestId;
  s.text = '';

  post(s, { type: 'start', title: `${fileName} · ${model}` });
  if (truncated) {
    const warn = 'Код длиннее 12 000 символов и был обрезан — объяснение относится только к его началу.';
    void vscode.window.showWarningMessage(`Clinic AI: ${warn}`);
    post(s, { type: 'notice', text: warn });
  }

  try {
    await streamChat({
      baseUrl,
      model,
      messages,
      temperature,
      signal: controller.signal,
      onToken: (text) => {
        if (s.requestId !== requestId) {
          return;
        }
        s.text += text;
        post(s, { type: 'token', text });
      }
    });
    if (s.requestId === requestId) {
      post(s, { type: 'done' });
    }
  } catch (err) {
    if (s.requestId !== requestId) {
      return; // запрос уже заменён новым
    }
    if (isAbortError(err)) {
      post(s, { type: 'stopped' });
      return;
    }
    const message = describeError(err, { baseUrl, model });
    post(s, { type: 'error', text: message });
    void vscode.window.showErrorMessage(`Clinic AI: ${message}`);
  } finally {
    if (s.controller === controller) {
      s.controller = undefined;
    }
  }
}
````

### vscode-extension/media/panel.js

````js
// Скрипт webview. Весь текст модели выводится через textContent — HTML не интерпретируется.
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();
  const output = document.getElementById('output');
  const title = document.getElementById('title');
  const status = document.getElementById('status');
  const notice = document.getElementById('notice');
  const errorBox = document.getElementById('error');
  const stopBtn = document.getElementById('stop');
  const copyBtn = document.getElementById('copy');

  let text = '';

  function setRunning(running) {
    stopBtn.disabled = !running;
  }

  function render() {
    output.textContent = text;
    // Автопрокрутка вниз, пока идёт генерация.
    window.scrollTo(0, document.body.scrollHeight);
  }

  stopBtn.addEventListener('click', function () {
    vscode.postMessage({ type: 'stop' });
  });
  copyBtn.addEventListener('click', function () {
    vscode.postMessage({ type: 'copy' });
  });

  window.addEventListener('message', function (event) {
    const msg = event.data;
    // Проверка формы и типа сообщения.
    if (typeof msg !== 'object' || msg === null || typeof msg.type !== 'string') {
      return;
    }
    switch (msg.type) {
      case 'start':
        text = '';
        output.textContent = '';
        errorBox.hidden = true;
        errorBox.textContent = '';
        notice.hidden = true;
        notice.textContent = '';
        if (typeof msg.title === 'string') {
          title.textContent = 'Clinic AI: ' + msg.title;
        }
        status.textContent = 'Генерация…';
        copyBtn.disabled = true;
        setRunning(true);
        break;
      case 'notice':
        if (typeof msg.text === 'string') {
          notice.textContent = msg.text;
          notice.hidden = false;
        }
        break;
      case 'token':
        if (typeof msg.text === 'string') {
          text += msg.text;
          copyBtn.disabled = false;
          render();
        }
        break;
      case 'done':
        status.textContent = 'Готово';
        setRunning(false);
        break;
      case 'stopped':
        status.textContent = 'Остановлено';
        setRunning(false);
        break;
      case 'error':
        if (typeof msg.text === 'string') {
          errorBox.textContent = msg.text;
          errorBox.hidden = false;
        }
        status.textContent = 'Ошибка';
        setRunning(false);
        break;
      default:
        break;
    }
  });

  // Сообщаем расширению, что можно слать сообщения.
  vscode.postMessage({ type: 'ready' });
})();
````

### vscode-extension/media/panel.css

````css
body {
  padding: 0 16px 16px;
  color: var(--vscode-foreground);
  background: var(--vscode-editor-background);
  font-family: var(--vscode-font-family);
  font-size: var(--vscode-font-size);
}

header {
  position: sticky;
  top: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  background: var(--vscode-editor-background);
  border-bottom: 1px solid var(--vscode-panel-border);
}

#title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.buttons {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

button {
  padding: 4px 12px;
  color: var(--vscode-button-foreground);
  background: var(--vscode-button-background);
  border: 1px solid transparent;
  border-radius: 2px;
  cursor: pointer;
}

button:hover:not(:disabled) {
  background: var(--vscode-button-hoverBackground);
}

button:disabled {
  opacity: 0.5;
  cursor: default;
}

#notice {
  margin-top: 10px;
  padding: 6px 10px;
  border-left: 3px solid var(--vscode-editorWarning-foreground);
  background: var(--vscode-inputValidation-warningBackground);
}

#error {
  margin-top: 10px;
  padding: 6px 10px;
  border-left: 3px solid var(--vscode-errorForeground);
  background: var(--vscode-inputValidation-errorBackground);
  white-space: pre-wrap;
}

/* Markdown показывается как обычный моноширинный текст с переносами. */
#output {
  margin: 12px 0 0;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: var(--vscode-editor-font-size, 13px);
  line-height: 1.5;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
}

#status {
  margin-top: 8px;
  color: var(--vscode-descriptionForeground);
  font-size: 0.9em;
}
````

### vscode-extension/test/prompt.test.ts

````ts
import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  DEFAULT_SYSTEM_PROMPT,
  MAX_CODE_LENGTH,
  buildMessages,
  chooseFence,
  pickContextRange,
  truncateCode
} from '../src/prompt';

describe('truncateCode', () => {
  it('не трогает короткий код', () => {
    assert.deepEqual(truncateCode('abc', 10), { code: 'abc', truncated: false });
  });

  it('код ровно в лимит не считается обрезанным', () => {
    const code = 'x'.repeat(MAX_CODE_LENGTH);
    const r = truncateCode(code);
    assert.equal(r.truncated, false);
    assert.equal(r.code.length, MAX_CODE_LENGTH);
  });

  it('обрезает длинный код до лимита', () => {
    const r = truncateCode('x'.repeat(MAX_CODE_LENGTH + 500));
    assert.equal(r.truncated, true);
    assert.equal(r.code.length, MAX_CODE_LENGTH);
  });

  it('не разрезает суррогатную пару', () => {
    const r = truncateCode('a😀b', 2); // 'a' + старшая половина эмодзи
    assert.equal(r.truncated, true);
    assert.equal(r.code, 'a');
  });
});

describe('pickContextRange', () => {
  it('середина файла: ±10 строк', () => {
    assert.deepEqual(pickContextRange(100, 50), { start: 40, end: 60 });
  });

  it('у начала файла окно урезается', () => {
    assert.deepEqual(pickContextRange(100, 3), { start: 0, end: 13 });
    assert.deepEqual(pickContextRange(100, 0), { start: 0, end: 10 });
  });

  it('у конца файла окно урезается', () => {
    assert.deepEqual(pickContextRange(100, 97), { start: 87, end: 99 });
    assert.deepEqual(pickContextRange(100, 99), { start: 89, end: 99 });
  });

  it('короткий файл целиком', () => {
    assert.deepEqual(pickContextRange(5, 2), { start: 0, end: 4 });
  });

  it('курсор вне диапазона зажимается; пустой файл не падает', () => {
    assert.deepEqual(pickContextRange(20, 500), { start: 9, end: 19 });
    assert.deepEqual(pickContextRange(20, -5), { start: 0, end: 10 });
    assert.deepEqual(pickContextRange(0, 0), { start: 0, end: 0 });
  });
});

describe('chooseFence', () => {
  it('по умолчанию три обратные кавычки', () => {
    assert.equal(chooseFence('const a = 1;'), '```');
  });

  it('удлиняется, если код сам содержит ```', () => {
    assert.equal(chooseFence('x\n```\ny'), '````');
  });
});

describe('buildMessages', () => {
  it('использует встроенный промпт, если настройка пуста', () => {
    const r = buildMessages({ systemPrompt: '  ', languageId: 'ts', fileName: 'a.ts', code: 'let a = 1;' });
    assert.equal(r.messages[0].role, 'system');
    assert.equal(r.messages[0].content, DEFAULT_SYSTEM_PROMPT);
    assert.equal(r.truncated, false);
  });

  it('использует пользовательский промпт', () => {
    const r = buildMessages({ systemPrompt: 'Отвечай одним словом', languageId: 'py', fileName: 'a.py', code: 'x' });
    assert.equal(r.messages[0].content, 'Отвечай одним словом');
  });

  it('кладёт код в блок с языком и указывает файл', () => {
    const r = buildMessages({ languageId: 'python', fileName: 'main.py', code: 'print(1)' });
    const user = r.messages[1];
    assert.equal(user.role, 'user');
    assert.match(user.content, /Файл: main\.py/);
    assert.match(user.content, /Язык: python/);
    assert.match(user.content, /```python\nprint\(1\)\n```/);
  });

  it('помечает обрезку и режет код', () => {
    const r = buildMessages({ languageId: 'c', fileName: 'big.c', code: 'y'.repeat(MAX_CODE_LENGTH + 10) });
    assert.equal(r.truncated, true);
    assert.match(r.messages[1].content, /обрезан/);
    assert.ok(!r.messages[1].content.includes('y'.repeat(MAX_CODE_LENGTH + 1)));
  });
});
````

### vscode-extension/test/ollama.test.ts

````ts
import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import { NdjsonParser, OllamaError, StreamEvent, describeError, streamChat } from '../src/ollama';

const line = (obj: unknown): string => JSON.stringify(obj) + '\n';
const tok = (s: string, done = false): string => line({ message: { role: 'assistant', content: s }, done });

function textOf(events: StreamEvent[]): string {
  return events.map((e) => (e.type === 'token' ? e.text : '')).join('');
}

describe('NdjsonParser', () => {
  it('разбирает несколько строк в одном чанке', () => {
    const p = new NdjsonParser();
    const ev = p.push(tok('При') + tok('вет') + line({ done: true }));
    assert.equal(textOf(ev), 'Привет');
    assert.deepEqual(ev[ev.length - 1], { type: 'done' });
  });

  it('склеивает строку, разбитую между чанками', () => {
    const p = new NdjsonParser();
    const full = tok('Hello');
    const cut = 12;
    assert.deepEqual(p.push(full.slice(0, cut)), []);
    const ev = p.push(full.slice(cut));
    assert.deepEqual(ev, [{ type: 'token', text: 'Hello' }]);
  });

  it('работает при побайтовой подаче', () => {
    const p = new NdjsonParser();
    const all = tok('a') + tok('b') + tok('', true);
    const events: StreamEvent[] = [];
    for (const ch of all) {
      events.push(...p.push(ch));
    }
    assert.equal(textOf(events), 'ab');
    assert.equal(events[events.length - 1].type, 'done');
  });

  it('пропускает пустые строки и CRLF', () => {
    const p = new NdjsonParser();
    const ev = p.push('\n\r\n' + tok('x').replace('\n', '\r\n') + '   \n');
    assert.deepEqual(ev, [{ type: 'token', text: 'x' }]);
  });

  it('строка без завершающего \\n выдаётся через flush', () => {
    const p = new NdjsonParser();
    assert.deepEqual(p.push(JSON.stringify({ message: { content: 'z' }, done: false })), []);
    assert.deepEqual(p.flush(), [{ type: 'token', text: 'z' }]);
  });

  it('ошибка в потоке превращается в событие error', () => {
    const p = new NdjsonParser();
    const ev = p.push(tok('a') + line({ error: 'model runner crashed' }));
    assert.deepEqual(ev, [
      { type: 'token', text: 'a' },
      { type: 'error', message: 'model runner crashed' }
    ]);
  });

  it('done: true завершает разбор, последующие строки игнорируются', () => {
    const p = new NdjsonParser();
    const ev = p.push(tok('', true) + tok('лишнее'));
    assert.deepEqual(ev, [{ type: 'done' }]);
  });

  it('токен из финального объекта с done: true не теряется', () => {
    const p = new NdjsonParser();
    assert.deepEqual(p.push(tok('end', true)), [
      { type: 'token', text: 'end' },
      { type: 'done' }
    ]);
  });

  it('битый JSON даёт error', () => {
    const p = new NdjsonParser();
    const ev = p.push('{not json\n');
    assert.equal(ev.length, 1);
    assert.equal(ev[0].type, 'error');
  });
});

function fakeFetch(chunks: string[], status = 200): typeof fetch {
  return (async () => {
    const enc = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) {
          controller.enqueue(enc.encode(c));
        }
        controller.close();
      }
    });
    return new Response(body, { status });
  }) as unknown as typeof fetch;
}

const base = { baseUrl: 'http://127.0.0.1:11434/', model: 'm', messages: [], temperature: 0.2 };

describe('streamChat', () => {
  it('собирает токены из чанков, разрезающих строки и UTF-8', async () => {
    const all = tok('Привет, ') + tok('мир') + line({ done: true });
    const bytes = new TextEncoder().encode(all);
    // режем по байтам, чтобы кириллица разорвалась посередине символа
    const parts: string[] = [];
    const chunks: Uint8Array[] = [];
    for (let i = 0; i < bytes.length; i += 7) {
      chunks.push(bytes.slice(i, i + 7));
    }
    const f = (async () => {
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          chunks.forEach((x) => c.enqueue(x));
          c.close();
        }
      });
      return new Response(body, { status: 200 });
    }) as unknown as typeof fetch;
    await streamChat({ ...base, fetchImpl: f, onToken: (t) => parts.push(t) });
    assert.equal(parts.join(''), 'Привет, мир');
  });

  it('ошибка в потоке бросает OllamaError(stream)', async () => {
    const f = fakeFetch([tok('a'), line({ error: 'boom' })]);
    await assert.rejects(
      streamChat({ ...base, fetchImpl: f, onToken: () => undefined }),
      (e: unknown) => e instanceof OllamaError && e.kind === 'stream' && e.message === 'boom'
    );
  });

  it('поток без done считается оборванным', async () => {
    const f = fakeFetch([tok('a')]);
    await assert.rejects(streamChat({ ...base, fetchImpl: f, onToken: () => undefined }), OllamaError);
  });

  it('404 → model-missing', async () => {
    const f = fakeFetch([JSON.stringify({ error: 'model "m" not found' })], 404);
    await assert.rejects(
      streamChat({ ...base, fetchImpl: f, onToken: () => undefined }),
      (e: unknown) => e instanceof OllamaError && e.kind === 'model-missing'
    );
  });

  it('ECONNREFUSED → unreachable', async () => {
    const f = (async () => {
      const err = new TypeError('fetch failed');
      (err as { cause?: unknown }).cause = { code: 'ECONNREFUSED' };
      throw err;
    }) as unknown as typeof fetch;
    await assert.rejects(
      streamChat({ ...base, fetchImpl: f, onToken: () => undefined }),
      (e: unknown) => e instanceof OllamaError && e.kind === 'unreachable'
    );
  });
});

describe('describeError', () => {
  const ctx = { baseUrl: 'http://127.0.0.1:11434', model: 'qwen2.5-coder:3b' };

  it('подсказывает команду запуска', () => {
    assert.match(describeError(new OllamaError('unreachable', ''), ctx), /ollama serve/);
  });

  it('подсказывает ollama pull', () => {
    assert.match(
      describeError(new OllamaError('model-missing', ''), ctx),
      /ollama pull qwen2\.5-coder:3b/
    );
  });
});
````

### vscode-extension/README.md

````markdown
# Clinic AI: Explain Code

Учебное расширение VS Code (publisher `chetverikov-lab`): объясняет выделенный код на русском языке с помощью локальной модели [Ollama](https://ollama.com). Ответ выводится в боковой панели по мере генерации. Внешних зависимостей во время выполнения нет.

## Требования

- VS Code 1.90 или новее.
- Node.js 18+ (для сборки и тестов).
- Запущенный Ollama и загруженная модель:

```bash
ollama serve
ollama pull qwen2.5-coder:3b
```

## Сборка и установка (.vsix)

```bash
npm install
npm run compile
npm run package        # создаст clinic-ai-explain-code-0.1.0.vsix
code --install-extension clinic-ai-explain-code-0.1.0.vsix
```

Либо в VS Code: панель расширений → меню «…» → «Install from VSIX…».

Для отладки: открыть папку в VS Code и нажать F5 (потребуется стандартная конфигурация запуска Extension Host).

## Использование

1. Выделите код (если выделения нет, берутся текущая строка и по 10 строк вокруг).
2. Вызовите команду «Clinic AI: Объясни этот код»:
   - `Ctrl+Alt+E` (`Cmd+Alt+E` на macOS);
   - пункт контекстного меню редактора;
   - кнопка в заголовке редактора (видна при наличии выделения).
3. Ответ появится в панели рядом с редактором. Кнопки: «Стоп» (прерывает генерацию) и «Копировать».
4. Повторный вызов команды использует ту же панель и отменяет предыдущую генерацию.

Код длиннее 12 000 символов обрезается, об этом выводится предупреждение.

## Настройки

| Параметр | По умолчанию | Описание |
|---|---|---|
| `clinicAi.ollamaUrl` | `http://127.0.0.1:11434` | Адрес сервера Ollama |
| `clinicAi.model` | `qwen2.5-coder:3b` | Модель |
| `clinicAi.systemPrompt` | пусто | Свой системный промпт (пусто — встроенный) |
| `clinicAi.temperature` | `0.2` | Температура, 0..1 |

## Ошибки

- Ollama не запущена (`ECONNREFUSED` / `fetch failed`) — сообщение предложит команду `ollama serve`.
- Модель не найдена (HTTP 404) — сообщение предложит `ollama pull <модель>`.

## Безопасность панели

Content-Security-Policy: `default-src 'none'`, скрипты только с nonce, стили только из каталога `media` расширения. `localResourceRoots` ограничен каталогом `media`. Текст модели вставляется через `textContent`, HTML не интерпретируется. Сообщения webview ↔ расширение проверяются по типу.

## Тесты

Тесты на `node:test` не требуют запуска VS Code (проверяют `prompt.ts` и разбор NDJSON в `ollama.ts`):

```bash
npm install
npm test
```

Скрипт `pretest` компилирует `src` и `test` в `out-test/`, затем `node --test` запускает скомпилированные тесты.

## Структура

- `src/extension.ts` — активация, команда, панель;
- `src/ollama.ts` — потоковый клиент `/api/chat`, разбор NDJSON;
- `src/prompt.ts` — сборка сообщений, обрезка, контекст вокруг курсора;
- `media/panel.js`, `media/panel.css` — скрипт и стили панели;
- `test/*.test.ts` — модульные тесты.
````

## Допущения

- Сборка основного кода идёт из `src` в `out` (`main: ./out/extension.js`). Тесты компилируются отдельным `tsconfig.test.json` в `out-test`, чтобы не нарушить путь `main`.
- Контекст при пустом выделении: строка курсора ±10 строк. У границ файла окно урезается, а не сдвигается, то есть может быть меньше 21 строки.
- Имя файла и язык передаются в пользовательском сообщении. Поэтому они учитываются и при кастомном `clinicAi.systemPrompt`.
- Копирование идёт через `vscode.env.clipboard` из расширения: `navigator.clipboard` в webview ненадёжен. Расширение хранит накопленный текст ответа.
- Панель создаётся один раз. До сообщения `ready` от webview сообщения копятся в очереди и затем отправляются.
- Разрыв потока без `done: true` считается ошибкой.
- Пустой `systemPrompt` (только пробелы) означает встроенный промпт.
- В `package.json` нет поля `repository`, поэтому `package` вызывает `vsce` с `--allow-missing-repository`. Иконки расширения нет.
- Версии devDependencies указаны ориентировочно; при `npm install` могут подтянуться более новые.

## Что проверить на ревью

- Тесты не запускались и код не компилировался. Нужно выполнить `npm install && npm test && npm run compile`.
- Типы `Response`/`fetch`/`ReadableStream` берутся из `@types/node` ^20. Если `tsc` ругается на `fetch`, уточнить версию `@types/node` или добавить `"lib": ["ES2022", "DOM"]`.
- В `package.json` у `editor/title` группа `navigation` и условие `editorHasSelection`. Проверить, что кнопка появляется только при выделении.
- Поведение при смене модели или URL посреди генерации. Сейчас настройки читаются в момент вызова команды.
- Корректность `retainContextWhenHidden: true` (память) и реакция на закрытие панели во время стрима. Закрытие вызывает `abort()`.
- Размер окна контекста: 12 000 символов могут не поместиться в контекст `qwen2.5-coder:3b` при стандартном `num_ctx` Ollama. Тогда начало промпта обрезается молча, возможно, стоит передавать `options.num_ctx`.
- Тест побайтового разбиения UTF-8 в `streamChat` зависит от `TextDecoder` с `stream: true`. Убедиться, что на целевой версии Node он проходит.
