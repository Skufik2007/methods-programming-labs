// Демонстрация «движка» расширения без UI VS Code: те же скомпилированные модули
// (out/prompt.js, out/ollama.js), что вызывает команда clinicAi.explainCode, на
// настоящей Ollama. Запуск из vscode-extension/:  npm run compile && node demo/run-demo.mjs
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
const { buildMessages } = require('../out/prompt.js');
const { streamChat } = require('../out/ollama.js');

// Фрагмент из приложения лабораторной: проверка пересечения интервалов записей.
const source = readFileSync(new URL('../../app/services/scheduling.py', import.meta.url), 'utf8');
const start = source.indexOf('def intervals_overlap');
const code = source.slice(start, source.indexOf('\n\n\n', start));

const { messages } = buildMessages({ languageId: 'python', fileName: 'app/services/scheduling.py', code });
let answer = '';
let tokens = 0;
const started = Date.now();
await streamChat({
  baseUrl: 'http://127.0.0.1:11434',
  model: process.env.MODEL ?? 'qwen2.5-coder:3b',
  messages,
  temperature: 0.2,
  onToken: (t) => { answer += t; tokens += 1; process.stdout.write(t); },
});
const seconds = ((Date.now() - started) / 1000).toFixed(1);
const report = [
  '# Демонстрация: «Объясни этот код» на локальной модели',
  '',
  `Модель: ${process.env.MODEL ?? 'qwen2.5-coder:3b'}; потоковых фрагментов: ${tokens}; время: ${seconds} с.`,
  '',
  '## Системный промпт (по умолчанию)',
  '',
  '```text', messages[0].content, '```',
  '',
  '## Сообщение пользователя (выделенный код)',
  '',
  messages[1].content,
  '',
  '## Ответ модели (без правок)',
  '',
  answer.trim(),
  '',
].join('\n');
writeFileSync(new URL('./demo-output.md', import.meta.url), report);
console.log(`\n\n[${tokens} фрагментов за ${seconds} с, сохранено в demo/demo-output.md]`);
