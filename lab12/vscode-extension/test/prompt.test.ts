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
