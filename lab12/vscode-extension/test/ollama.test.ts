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
