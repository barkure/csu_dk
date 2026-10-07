const thanks = ['谢谢!', '謝謝!', 'Thank you!', 'ありがとう!'];
const heading = document.getElementById('thanks');

if (heading) {
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let index = 0;
  const next = () => {
    index = (index + 1) % thanks.length;
    heading.textContent = thanks[index];
  };
  setInterval(() => {
    if (reducedMotion.matches) {
      next();
      return;
    }
    heading.style.opacity = '0';
    setTimeout(() => {
      next();
      heading.style.opacity = '1';
    }, 400);
  }, 2000);
}
