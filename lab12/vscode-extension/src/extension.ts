import { randomBytes } from 'node:crypto';
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

/**
 * Nonce для CSP панели. Он защищает, только если его нельзя угадать, поэтому
 * берётся криптостойкий генератор, а не Math.random().
 */
function getNonce(): string {
  return randomBytes(18).toString('base64');
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
