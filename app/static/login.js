

function startCountdown(button) {
  let left = Number(button.dataset.cooldown || 0);
  if (!left || button.dataset.counting) return;
  button.dataset.counting = '1';
  button.disabled = true;

  const tick = () => {
    if (left <= 0) {
      clearInterval(timer);
      button.disabled = false;
      delete button.dataset.counting;
      button.textContent = '获取验证码';
      return;
    }
    button.textContent = `${left}s 后可重发`;
    left -= 1;
  };

  const timer = setInterval(tick, 1000);
  tick();
}

function startAllCountdowns(root) {
  (root || document).querySelectorAll('[data-cooldown]').forEach(startCountdown);
}

document.addEventListener('htmx:after:swap', (event) => startAllCountdowns(event.target));
document.addEventListener('DOMContentLoaded', () => startAllCountdowns(document));
