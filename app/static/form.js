
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
