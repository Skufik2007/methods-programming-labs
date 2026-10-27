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
