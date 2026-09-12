/* 坐标转换：浏览器定位给的是 WGS-84，学校收 GCJ-02。经典脚本，函数挂在全局。 */
const PI = Math.PI;
const A = 6378245.0;
const EE = 0.00669342162296594323;

function outOfChina(lng, lat) {
  return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55);
}

function transformLat(x, y) {
  let ret = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  ret += ((20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2) / 3;
  ret += ((20 * Math.sin(y * PI) + 40 * Math.sin((y / 3) * PI)) * 2) / 3;
  ret += ((160 * Math.sin((y / 12) * PI) + 320 * Math.sin((y * PI) / 30)) * 2) / 3;
  return ret;
}

function transformLng(x, y) {
  let ret = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  ret += ((20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2) / 3;
  ret += ((20 * Math.sin(x * PI) + 40 * Math.sin((x / 3) * PI)) * 2) / 3;
  ret += ((150 * Math.sin((x / 12) * PI) + 300 * Math.sin((x / 30) * PI)) * 2) / 3;
  return ret;
}

function wgs84ToGcj02(lng, lat) {
  if (outOfChina(lng, lat)) return { jd: lng, wd: lat };
  let dLat = transformLat(lng - 105.0, lat - 35.0);
  let dLng = transformLng(lng - 105.0, lat - 35.0);
  const radLat = (lat / 180.0) * PI;
  let magic = Math.sin(radLat);
  magic = 1 - EE * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / (((A * (1 - EE)) / (magic * sqrtMagic)) * PI);
  dLng = (dLng * 180.0) / ((A / sqrtMagic) * Math.cos(radLat) * PI);
  return { jd: lng + dLng, wd: lat + dLat };
}

// —— 与页面绑定的部分：点「使用当前位置」→ 浏览器定位 → WGS-84 转 GCJ-02 → 填进输入框
/* 坐标相关的临时文字（定位结果、ⓘ 展开的说明）都在 10 秒后自行消失 ——
   都是看一眼就够的东西，留在表单里只会挤占空间。 */
const TEMP_NOTE_MS = 10000;
let noteTimer = null;
let hintTimer = null;

function setNote(text, autoHide = true) {
  const note = document.getElementById('coords-note');
  if (!note) return;
  clearTimeout(noteTimer);
  note.textContent = text;
  // “正在定位…”这类过程提示不设倒计时：慢的时候会在结果回来之前就被清掉
  if (autoHide) noteTimer = setTimeout(() => { note.textContent = ''; }, TEMP_NOTE_MS);
}

function toggleHint() {
  const hint = document.getElementById('coords-hint');
  const toggle = document.getElementById('coords-hint-toggle');
  if (!hint || !toggle) return;
  const opened = !hint.classList.toggle('hidden');
  toggle.setAttribute('aria-expanded', String(opened));
  clearTimeout(hintTimer);
  if (opened) {
    hintTimer = setTimeout(() => {
      hint.classList.add('hidden');
      toggle.setAttribute('aria-expanded', 'false');
    }, TEMP_NOTE_MS);
  }
}

function pickHere() {
  const input = document.getElementById('coords');
  if (!navigator.geolocation) { setNote('当前环境不提供定位'); return; }
  setNote('正在定位…', false);
  navigator.geolocation.getCurrentPosition((pos) => {
    const { jd, wd } = wgs84ToGcj02(pos.coords.longitude, pos.coords.latitude);
    input.value = `${jd.toFixed(6)},${wd.toFixed(6)}`;
    setNote(`已用当前定位（精度 ±${Math.round(pos.coords.accuracy)} 米，已按 WGS-84 → GCJ-02 转换）`);
  }, () => { setNote('定位失败或被拒绝，请手填经纬度'); }, { enableHighAccuracy: true, timeout: 10000 });
}
document.addEventListener('click', (event) => {
  if (event.target && event.target.id === 'pick-here') pickHere();
  if (event.target && event.target.closest && event.target.closest('#coords-hint-toggle')) toggleHint();
});
