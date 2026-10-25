
document.addEventListener('click', (event) => {
  const button = event.target.closest && event.target.closest('[data-reveal]');
  if (!button) return;

  const input = button.parentElement && button.parentElement.querySelector('input');
  if (!input) return;

  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  button.setAttribute('aria-pressed', String(show));
  button.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
  button.title = show ? '隐藏密码' : '显示密码';
  input.focus();
});

document.addEventListener('click', event => {
  const button = event.target.closest('[data-edit-time]');
  if (!button) return;
  const input = document.getElementById(button.dataset.editTime);
  const editing = input.classList.contains('hidden');
  input.classList.toggle('hidden', !editing);
  input.previousElementSibling.classList.toggle('hidden', editing);
  button.setAttribute('aria-expanded', String(editing));
  if (editing) {
    try {
      if (window.matchMedia('(pointer: coarse)').matches || !input.showPicker) input.focus();
      else {
        input.focus();
        input.showPicker();
      }
    } catch {
      input.focus();
    }
  }
});

document.addEventListener('focusout', event => {
  const input = event.target;
  if (!input.matches('input[name="notBefore"]') || input.value !== input.defaultValue) return;
  if (!event.relatedTarget) return;
  const button = document.querySelector(`[data-edit-time="${input.id}"]`);
  if (event.relatedTarget === button) return;
  input.classList.add('hidden');
  input.previousElementSibling.classList.remove('hidden');
  button.setAttribute('aria-expanded', 'false');
});

document.addEventListener('htmx:before:swap', event => {
  const input = document.activeElement;
  if (input?.matches('input[name="notBefore"]') &&
      event.detail.tasks.some(task => task.target?.contains(input))) input.blur();
});

document.addEventListener('htmx:after:swap', event => {
  const message = event.detail.ctx.response?.raw.headers.get('X-Not-Before-Error');
  if (message) window.alert(JSON.parse(message));
});
